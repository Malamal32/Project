// LinkedIn data-export reader. Runs entirely in this tab.
//
// Opens the .zip LinkedIn emails a student ("Settings → Get a copy of your
// data") and hands back only the members `service/linkedin_import.py` reads.
// Everything else in the archive — Connections.csv, messages.csv, ad-targeting
// segments, inferred attributes — is never decompressed and never leaves this
// machine.
//
// That split is the same one `parseTranscript` makes with pdf.js, but the
// reason is stronger. A transcript is the student's own document; a LinkedIn
// archive contains other people's names, employers and private messages, and
// uploading it whole so a server could pick five files out of it would mean
// putting all of that on the wire to save about eighty lines of code here.
//
// No zip library. `DecompressionStream('deflate-raw')` is native in every
// browser this product supports, so the only thing missing was the container
// format, and a ZIP central directory is a fixed-layout record. Vendoring a
// library for it would be a third thing in frontend/vendor and a build-step
// argument waiting to happen.

export class LinkedInImportError extends Error {}

// Mirrors the keys of EXPORT_FILES in `service/linkedin_import.py`. This list
// is a privacy boundary, not a convenience: `tests/test_linkedin_parity.py`
// runs this module under node and fails if the two ever disagree. If they
// drift, the browser starts sending members the server never asked for.
export const WANTED_FILES = new Set([
  'positions.csv',
  'projects.csv',
  'skills.csv',
  'certifications.csv',
  'honors.csv',
  'honours.csv'
]);

// A student's whole export, including the parts we never open. Real archives
// run to a few megabytes; this is only here so a mis-picked file cannot pull a
// gigabyte into a tab before anything has looked at it.
const MAX_ARCHIVE_BYTES = 64 * 1024 * 1024;

const EOCD_SIGNATURE = 0x06054b50;
const CENTRAL_SIGNATURE = 0x02014b50;
const LOCAL_SIGNATURE = 0x04034b50;

const EOCD_MIN_SIZE = 22;
const MAX_ZIP_COMMENT = 0xffff;

// The two compression methods a real .zip uses. Anything else (bzip2, LZMA,
// zstd) is rejected by name rather than mis-inflated into gibberish that would
// then be reported as a malformed CSV.
const METHOD_STORED = 0;
const METHOD_DEFLATE = 8;

// Zip64 sentinels. LinkedIn archives are nowhere near 4 GB or 65 535 entries,
// so rather than implement the Zip64 extensions we detect them and say so —
// guessing past a sentinel would read the wrong bytes and blame the CSV.
const ZIP64_U16 = 0xffff;
const ZIP64_U32 = 0xffffffff;

/** Basename, lowercased. Mirrors `member_basename` in the Python module: some
 *  archives store full paths, and zips built on Windows use backslashes. */
export function memberBasename(name) {
  return name.replace(/\\/g, '/').split('/').pop().trim().toLowerCase();
}

/** Locate the End Of Central Directory record, scanning back from the end past
 *  a possible trailing comment. */
function findEndOfCentralDirectory(view) {
  const earliest = Math.max(0, view.byteLength - EOCD_MIN_SIZE - MAX_ZIP_COMMENT);
  for (let at = view.byteLength - EOCD_MIN_SIZE; at >= earliest; at--) {
    if (view.getUint32(at, true) === EOCD_SIGNATURE) return at;
  }
  return -1;
}

/** The central directory: one record per member, which is the only place the
 *  compressed size is trustworthy — a local header may carry zeros and defer
 *  the real sizes to a data descriptor after the payload. */
function readCentralDirectory(view, bytes) {
  const eocd = findEndOfCentralDirectory(view);
  if (eocd < 0) {
    throw new LinkedInImportError("That doesn't look like a .zip archive.");
  }

  const entryCount = view.getUint16(eocd + 10, true);
  const directoryOffset = view.getUint32(eocd + 16, true);
  if (entryCount === ZIP64_U16 || directoryOffset === ZIP64_U32) {
    throw new LinkedInImportError('That archive uses a zip format this importer cannot read.');
  }

  const entries = [];
  let at = directoryOffset;
  for (let i = 0; i < entryCount; i++) {
    if (at + 46 > view.byteLength || view.getUint32(at, true) !== CENTRAL_SIGNATURE) {
      throw new LinkedInImportError('That archive appears to be damaged.');
    }
    const nameLength = view.getUint16(at + 28, true);
    const extraLength = view.getUint16(at + 30, true);
    const commentLength = view.getUint16(at + 32, true);
    entries.push({
      name: new TextDecoder().decode(bytes.subarray(at + 46, at + 46 + nameLength)),
      method: view.getUint16(at + 10, true),
      compressedSize: view.getUint32(at + 20, true),
      localOffset: view.getUint32(at + 42, true)
    });
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

/** The member's payload, using the *local* header's own name and extra lengths
 *  — they routinely differ from the central directory's, and using the wrong
 *  ones lands a few bytes into the compressed stream. */
function payloadOf(view, bytes, entry) {
  const at = entry.localOffset;
  if (at + 30 > view.byteLength || view.getUint32(at, true) !== LOCAL_SIGNATURE) {
    throw new LinkedInImportError('That archive appears to be damaged.');
  }
  const nameLength = view.getUint16(at + 26, true);
  const extraLength = view.getUint16(at + 28, true);
  const start = at + 30 + nameLength + extraLength;
  return bytes.subarray(start, start + entry.compressedSize);
}

async function inflateToText(payload, method) {
  if (method === METHOD_STORED) return new TextDecoder().decode(payload);
  if (method !== METHOD_DEFLATE) {
    throw new LinkedInImportError('That archive uses a compression method this importer cannot read.');
  }
  // `Response.text()` decodes UTF-8 for us, so the inflated bytes are never
  // materialized as a second copy.
  const stream = new Blob([payload]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
  return await new Response(stream).text();
}

/**
 * Read a LinkedIn export.
 *
 * Accepts the whole `.zip`, or a single `.csv` for students who used LinkedIn's
 * "Want something in particular?" download and got the files loose. Either way
 * the result is the same map the import endpoint takes: member name -> CSV text,
 * containing only names in WANTED_FILES.
 *
 * Returns `{ files, skipped }`. `skipped` is a count, not a list — naming the
 * members we deliberately did not open would put the archive's table of
 * contents into the page, which is most of what we were avoiding sending.
 */
export async function readExport(file) {
  if (!file) throw new LinkedInImportError('No file was selected.');
  if (file.size > MAX_ARCHIVE_BYTES) {
    throw new LinkedInImportError('That file is too large to be a LinkedIn export.');
  }

  const name = memberBasename(file.name || '');

  if (name.endsWith('.csv')) {
    if (!WANTED_FILES.has(name)) {
      throw new LinkedInImportError(
        'That CSV is not one this importer reads. Pick Positions.csv, or the whole archive.'
      );
    }
    return { files: { [name]: await file.text() }, skipped: 0 };
  }

  if (!name.endsWith('.zip')) {
    throw new LinkedInImportError('Please pick the .zip archive LinkedIn sent you, or a single .csv from it.');
  }

  const bytes = new Uint8Array(await file.arrayBuffer());
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const entries = readCentralDirectory(view, bytes);

  const files = {};
  let skipped = 0;
  for (const entry of entries) {
    const member = memberBasename(entry.name);
    // The whitelist is applied before anything is decompressed, so a member we
    // do not read is never even expanded in memory.
    if (!WANTED_FILES.has(member) || member in files) {
      skipped++;
      continue;
    }
    files[member] = await inflateToText(payloadOf(view, bytes, entry), entry.method);
  }
  return { files, skipped };
}
