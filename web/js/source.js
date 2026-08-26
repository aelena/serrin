/**
 * Choosing what a piece is made of: a CSV you drop in, or a repository path.
 *
 * The pipeline is Python and offline, so the browser cannot transform anything
 * itself. Rather than porting nine pedals to JavaScript -- which would create a
 * second source of truth for the aesthetic, and two implementations that drift
 * with the *sound* as the thing that drifts -- the file is posted to the one
 * real pipeline and the rendered pair comes back.
 *
 * Consequence worth stating plainly: this only works while `scripts/serve.py` is
 * running. It is an author's tool, like the panel it lives in.
 */

/** Read a picked file as text. CSVs are text; there is nothing to decode. */
export function readTextFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`cannot read ${file.name}`));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsText(file);
  });
}

/**
 * Ask the server to render something.
 *
 * @param {object} request {csv, name} or {repo}, plus preset/tempo/metric...
 * @returns {Promise<object>} the render's metadata and the URLs to load
 */
export async function requestRender(request) {
  let response;
  try {
    response = await fetch('/api/render', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
  } catch (error) {
    // A network-level failure here almost always means one thing.
    throw new Error(
      `cannot reach the render endpoint (${error.message}) -- ` +
        'is scripts/serve.py running?',
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`the server replied with ${response.status} and no JSON`);
  }
  if (!response.ok || payload.error) {
    throw new Error(payload.error ?? `render failed (${response.status})`);
  }
  return payload;
}

/** One line describing what came back. */
export function describeRender(result) {
  const bits = [
    result.label,
    `${result.voices.length} voices`,
    `${result.frames} frames`,
    `${Number(result.duration).toFixed(1)}s`,
    `${Number(result.bars).toFixed(1)} bars`,
  ];
  if (result.git) {
    bits.push(
      `${result.git.commits} commits, ${result.git.merges} merges, ` +
        `trunk ${result.git.trunk}`,
    );
  }
  return bits.join(' · ');
}
