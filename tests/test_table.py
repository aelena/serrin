"""Finding the table inside a real-world export, and reading its numbers.

Every case here comes from a file that broke: a PVGIS timeseries whose header is
on line nine, a Solargis prospect with forty-one comment lines and semicolons,
and the decimal comma that turned 12,5 into 125 without complaining.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.ingest import (  # noqa: E402
    IngestError,
    _parse_number,
    find_table,
    read_rows,
    rows_from_text,
)


def _write(text: str, name: str = "t.csv") -> Path:
    folder = Path(tempfile.mkdtemp())
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


class FindTableTests(unittest.TestCase):
    def test_plain_file_starts_at_line_one(self):
        lines = ["a,b,c", "1,2,3", "4,5,6"]
        delimiter, header, start = find_table(lines)
        self.assertEqual((delimiter, header, start), (",", 0, 1))

    def test_metadata_preamble_is_skipped(self):
        # PVGIS: tab-separated key/value metadata, a blank, then the real table.
        lines = [
            "Latitude (decimal degrees):\t41.340",
            "Longitude (decimal degrees):\t-4.899",
            "Elevation (m):\t750",
            "",
            "time,G(i),H_sun,T2m",
            "20050101:0010,0.0,0.0,3.1",
            "20050101:0110,0.0,0.0,2.9",
            "20050101:0210,0.0,0.0,2.7",
        ]
        delimiter, header, start = find_table(lines)
        self.assertEqual(delimiter, ",")
        self.assertEqual(header, 4)
        self.assertEqual(start, 5)

    def test_comment_preamble_and_semicolons(self):
        # Solargis: '#' comments, then a semicolon table.
        lines = ["# Solargis prospect", "# site: Medina", "#"] + ["Month;GHIm;T24"] + [
            f"{m};{100 + m};{10 + m}" for m in range(1, 13)
        ]
        delimiter, header, start = find_table(lines)
        self.assertEqual(delimiter, ";")
        self.assertEqual(header, 3)

    def test_wider_table_wins_a_tie(self):
        # A long preamble of two-field tab pairs must not beat the real table
        # just by being longer. Both reach the agreement cap; width decides.
        lines = [f"key{i}\tvalue{i}" for i in range(80)]
        lines += ["a,b,c,d,e,f"] + [f"{i},{i},{i},{i},{i},{i}" for i in range(80)]
        delimiter, header, _ = find_table(lines)
        self.assertEqual(delimiter, ",")
        self.assertEqual(header, 80)

    def test_no_table_falls_back_to_first_real_line(self):
        lines = ["# only prose", "", "nothing tabular here at all"]
        _, header, start = find_table(lines)
        self.assertEqual((header, start), (2, 3))


class ReadRowsTests(unittest.TestCase):
    def test_footer_prose_is_dropped_not_padded(self):
        path = _write(
            "a,b,c\n1,2,3\n4,5,6\n"
            "PVGIS (c) European Union, 2001-2024\n"
            "Report generated on 12/03/2024\n"
        )
        header, rows = read_rows(path)
        self.assertEqual(header, ["a", "b", "c"])
        # Two rows, not four: a one-field footer line padded with blanks would
        # read as real, very flat data.
        self.assertEqual(len(rows), 2)

    def test_report_says_what_it_decided(self):
        path = _write(
            "# meta\n# more meta\nMonth;GHI;T24\n1;100;11\n2;110;12\ntrailing note\n"
        )
        report = {}
        read_rows(path, report=report)
        self.assertEqual(report["delimiter"], ";")
        self.assertEqual(report["header_line"], 3)
        self.assertEqual(report["preamble_lines"], 2)
        self.assertEqual(report["columns"], 3)
        self.assertEqual(report["data_rows"], 2)
        self.assertEqual(report["dropped_rows"], 1)
        self.assertTrue(report["named_header"])

    def test_numeric_first_row_is_not_a_header(self):
        path = _write("1,2,3\n4,5,6\n7,8,9\n")
        header, rows = read_rows(path)
        self.assertEqual(header, ["col0", "col1", "col2"])
        self.assertEqual(len(rows), 3)  # the first row is data, not names
        report = {}
        read_rows(path, report=report)
        self.assertFalse(report["named_header"])

    def test_header_with_no_data_under_it_is_an_error(self):
        path = _write("# notes\na,b,c\n")
        with self.assertRaises(IngestError) as caught:
            read_rows(path)
        # The message has to name what it found, or there is nothing to act on.
        message = str(caught.exception)
        self.assertIn("line 2", message)
        self.assertIn("3 columns", message)

    def test_empty_file_is_an_error(self):
        with self.assertRaises(IngestError):
            read_rows(_write("\n\n  \n"))

    def test_blank_lines_inside_the_table_do_not_end_it(self):
        path = _write("a,b\n1,2\n\n3,4\n\n5,6\n")
        _, rows = read_rows(path)
        self.assertEqual(len(rows), 3)


class ParseNumberTests(unittest.TestCase):
    def test_decimal_comma(self):
        # The one that mattered: 12,5 read as 125 is a tenfold error in a column
        # that looks perfectly plausible afterwards.
        self.assertEqual(_parse_number("12,5"), 12.5)
        self.assertEqual(_parse_number("-0,75"), -0.75)

    def test_thousands_separator(self):
        self.assertEqual(_parse_number("1,234"), 1234.0)
        self.assertEqual(_parse_number("1,234,567.89"), 1234567.89)

    def test_plain_numbers(self):
        self.assertEqual(_parse_number("42"), 42.0)
        self.assertEqual(_parse_number("3.14"), 3.14)
        self.assertEqual(_parse_number("1e3"), 1000.0)
        self.assertEqual(_parse_number("  7  "), 7.0)

    def test_units_are_forgiven(self):
        self.assertEqual(_parse_number("45 ms"), 45.0)
        self.assertEqual(_parse_number("3.2 GB"), 3.2)

    def test_unreadable_cells(self):
        self.assertIsNone(_parse_number(""))
        self.assertIsNone(_parse_number("   "))
        self.assertIsNone(_parse_number("n/a"))
        self.assertIsNone(_parse_number("--"))


class RowsFromTextTests(unittest.TestCase):
    """Parsing contents rather than a path, so an upload can be checked first.

    A piece that points at a file nobody can parse is a worse failure than a
    refused upload: the refusal names the problem while the author still has the
    file in front of them and can go fix it.
    """

    def test_it_agrees_with_reading_the_same_file(self):
        # The whole reason it is a split-out body and not a second parser.
        text = "# meta\nMonth;GHI\n1;100\n2;110\n"
        path = _write(text)
        from_path = read_rows(path)
        from_text = rows_from_text(text)
        self.assertEqual(from_path, from_text)

    def test_the_report_is_the_same_too(self):
        text = "# a\n# b\nx,y\n1,2\n3,4\nfooter\n"
        by_path, by_text = {}, {}
        read_rows(_write(text), report=by_path)
        rows_from_text(text, report=by_text)
        by_path.pop("delimiter"), by_text.pop("delimiter")
        self.assertEqual(by_path, by_text)

    def test_empty_contents_are_refused_with_the_name_given(self):
        with self.assertRaises(IngestError) as caught:
            rows_from_text("   \n\n", label="meteo.csv")
        self.assertIn("meteo.csv", str(caught.exception))

    def test_a_header_with_no_data_is_refused_with_the_name_given(self):
        with self.assertRaises(IngestError) as caught:
            rows_from_text("# notes\na,b,c\n", label="meteo.csv")
        self.assertIn("meteo.csv", str(caught.exception))

    def test_prose_with_no_table_is_refused(self):
        with self.assertRaises(IngestError):
            rows_from_text("this file is a note to myself, not data\n", label="notes.csv")

    def test_what_is_not_refused(self):
        # The bar is "can the table be found at all". A preamble, a semicolon, a
        # footer and a decimal comma are read fine and are none of the uploader's
        # business -- rejecting them would be rejecting files that work.
        text = (
            "# Solargis prospect\n# site: Medina\n#\n"
            "Month;GHIm;T24\n"
            + "".join(f"{m};{100 + m};{10 + m},5\n" for m in range(1, 13))
            + "Report generated 2024-03-12\n"
        )
        header, rows = rows_from_text(text, label="solargis.csv")
        self.assertEqual(header, ["Month", "GHIm", "T24"])
        self.assertEqual(len(rows), 12)
        self.assertEqual(_parse_number(rows[0][2]), 11.5)


class UploadValidationTests(unittest.TestCase):
    """The endpoint refuses before it writes.

    It used to say so in its docstring while only doing it for histories: a CSV
    was written first and complained about afterwards, so an unparseable one
    still became the piece's source.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import serve  # noqa: PLC0415

        self.serve = serve
        self.root = Path(tempfile.mkdtemp())
        self._original = serve.PIECES_DIR
        serve.PIECES_DIR = self.root
        from serrin.piece import new_piece  # noqa: PLC0415

        new_piece(self.root / "p", name="p")

    def tearDown(self):
        self.serve.PIECES_DIR = self._original
        import shutil  # noqa: PLC0415

        shutil.rmtree(self.root, ignore_errors=True)

    def _put(self, text: str, filename: str = "d.csv"):
        return self.serve.put_data_api(
            {"piece": "p", "filename": filename, "text": text}
        )

    def test_an_unparseable_csv_is_refused_and_not_written(self):
        with self.assertRaises(ValueError) as caught:
            self._put("# just a note\na,b,c\n")
        self.assertIn("no data rows", str(caught.exception))
        # And nothing landed: the piece is not left pointing at a bad file.
        self.assertEqual(list((self.root / "p").glob("*.csv")), [])

    def test_a_real_export_is_accepted_and_reported(self):
        text = (
            "Latitude:\t41.34\nLongitude:\t-4.899\n\n"
            "time,G(i),T2m\n"
            + "".join(f"2005010{i}:0010,{i * 11},{i + 3}\n" for i in range(1, 9))
            + "PVGIS (c) European Union\n"
        )
        result = self._put(text, "Timeseries_41.340.csv")
        self.assertTrue(result["ok"])
        table = result["source"]["table"]
        self.assertEqual(table["header_line"], 4)
        self.assertEqual(table["preamble_lines"], 3)
        self.assertEqual(table["dropped_rows"], 1)
        self.assertTrue((self.root / "p" / result["path"]).exists())

    def test_an_empty_upload_is_refused(self):
        with self.assertRaises(ValueError):
            self._put("   \n")

    def test_a_suffix_serrin_does_not_read_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self._put("a,b\n1,2\n", "sheet.xlsx")
        self.assertIn(".xlsx", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
