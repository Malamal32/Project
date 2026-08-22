// Real client-side PDF text extraction + academic normalization.
// Mirrors service/academic_extraction.py: rule-based only, never fabricates a
// field that isn't literally present in the document. Runs entirely in the
// browser — on this path the file is never uploaded, stored, or logged.
//
// This is the *fallback* path. `parseTranscript` in api-service.js posts the
// PDF to the service first, which runs Claude-based extraction; it only reaches
// this module when that service is unreachable. Same rules either way, lower
// quality here — see README, "Two extraction stages, one of them a fallback".

// Vendored locally (pdfjs-dist 4.6.82) rather than pulled from a CDN, so the
// app runs offline and a student's PDF never depends on a third-party host.
// Resolved against this module's own URL, not the document's: `workerSrc` is
// read by pdf.js relative to the page, so a bare relative path would break.
const PDFJS_URL = new URL('../../vendor/pdf.min.mjs', import.meta.url).href;
const PDFJS_WORKER_URL = new URL('../../vendor/pdf.worker.min.mjs', import.meta.url).href;
const MIN_EXTRACTED_CHARS = 40;

let _pdfjs = null;
async function loadPdfJs() {
  if (_pdfjs) return _pdfjs;
  const lib = await import(/* webpackIgnore: true */ PDFJS_URL);
  lib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
  _pdfjs = lib;
  return lib;
}

export class PdfValidationError extends Error {}

const MAX_BYTES = 15 * 1024 * 1024;

/** Client-side mirror of the service's validation checks. */
async function validatePdf(file) {
  if (!file.name || !file.name.toLowerCase().endsWith('.pdf')) throw new PdfValidationError('Only PDF files are accepted.');
  if (file.size === 0) throw new PdfValidationError('That file is empty.');
  if (file.size > MAX_BYTES) throw new PdfValidationError('File exceeds the 15 MB upload limit.');
  const head = new Uint8Array(await file.slice(0, 5).arrayBuffer());
  const magic = String.fromCharCode(...head);
  if (magic !== '%PDF-') throw new PdfValidationError('That file does not look like a valid PDF.');
}

/** Extracts the text layer, line by line, preserving vertical order. */
async function extractPdfText(file) {
  const pdfjs = await loadPdfJs();
  const data = new Uint8Array(await file.arrayBuffer());
  let doc;
  try {
    doc = await pdfjs.getDocument({ data, isEvalSupported: false, disableAutoFetch: true }).promise;
  } catch (err) {
    if (String(err && err.name) === 'PasswordException') {
      throw new PdfValidationError('This PDF is password-protected. Please upload an unprotected copy.');
    }
    throw new PdfValidationError('This PDF could not be read — it may be corrupted.');
  }
  const lines = [];
  for (let p = 1; p <= doc.numPages; p++) {
    const page = await doc.getPage(p);
    const content = await page.getTextContent();
    const rows = new Map();
    for (const item of content.items) {
      if (!item.str || !item.str.trim()) continue;
      const y = Math.round(item.transform[5]);
      if (!rows.has(y)) rows.set(y, []);
      rows.get(y).push(item.str);
    }
    [...rows.keys()].sort((a, b) => b - a).forEach(y => lines.push(rows.get(y).join(' ').replace(/\s+/g, ' ').trim()));
  }
  return lines.join('\n');
}

// ---- normalization (mirrors academic_extraction.py) ----

// Spelled-out degree names are unambiguous and match anywhere.
const DEGREE_PATTERNS = [
  [/\bBachelor\s+of\s+Science\s+in\s+Engineering\b/i, 'Bachelor of Science in Engineering', "Bachelor's"],
  [/\bBachelor\s+of\s+Science\b/i, 'Bachelor of Science', "Bachelor's"],
  [/\bBachelor\s+of\s+Arts\b/i, 'Bachelor of Arts', "Bachelor's"],
  [/\bBachelor\s+of\s+Business\s+Administration\b/i, 'Bachelor of Business Administration', "Bachelor's"],
  [/\bBachelor\s+of\s+Fine\s+Arts\b/i, 'Bachelor of Fine Arts', "Bachelor's"],
  [/\bBachelor\s+of\s+Engineering\b/i, 'Bachelor of Engineering', "Bachelor's"],
  [/\bMaster\s+of\s+Science\b/i, 'Master of Science', "Master's"],
  [/\bMaster\s+of\s+Arts\b/i, 'Master of Arts', "Master's"],
  [/\bMaster\s+of\s+Business\s+Administration\b/i, 'Master of Business Administration', "Master's"],
  [/\bAssociate\s+of\s+(?:Arts|Science|Applied Science)\b/i, 'Associate Degree', 'Associate'],
  [/\bDoctor\s+of\s+Philosophy\b/i, 'Doctor of Philosophy', 'Doctorate']
];

// Abbreviations are only credible with an explicit degree cue nearby, so
// ordinary skill text ("MS Project", "MS Office") and passing mentions
// ("interested in PhD programs") never fabricate a credential.
const DEGREE_ABBREVIATIONS = [
  ['BBA', 'Bachelor of Business Administration', "Bachelor's"],
  ['BFA', 'Bachelor of Fine Arts', "Bachelor's"],
  ['BSE', 'Bachelor of Science in Engineering', "Bachelor's"],
  ['BS', 'Bachelor of Science', "Bachelor's"],
  ['BA', 'Bachelor of Arts', "Bachelor's"],
  ['MBA', 'Master of Business Administration', "Master's"],
  ['MS', 'Master of Science', "Master's"],
  ['MA', 'Master of Arts', "Master's"],
  ['PHD', 'Doctor of Philosophy', 'Doctorate'],
  ['BACHELORS', "Bachelor's Degree", "Bachelor's"],
  ['BACHELOR', "Bachelor's Degree", "Bachelor's"],
  ['MASTERS', "Master's Degree", "Master's"],
  ['MASTER', "Master's Degree", "Master's"]
];

const DEGREE_CUE_BEFORE = /(?:degree|program(?:\s+of\s+study)?|major|awarded|conferred|candidate\s+for|pursuing)\s*[:\-]?\s*$/i;
const DEGREE_CUE_AFTER = /^\s*(?:in\b|degree\b|,\s*[A-Z])/i;

function abbrevPattern(abbrev) {
  // Allow optional periods/spaces between letters: BS, B.S., B S
  return abbrev.split('').map(ch => ch + '\\.?\\s?').join('').replace(/\\s\?$/, '');
}

function matchDegreeAbbreviation(text) {
  for (const [abbrev, name, level] of DEGREE_ABBREVIATIONS) {
    const re = new RegExp('\\b(' + abbrevPattern(abbrev) + ')\\b', 'gi');
    let m;
    while ((m = re.exec(text)) !== null) {
      const before = text.slice(Math.max(0, m.index - 40), m.index);
      const after = text.slice(m.index + m[0].length, m.index + m[0].length + 30);
      if (DEGREE_CUE_BEFORE.test(before) || DEGREE_CUE_AFTER.test(after)) {
        return [name, level];
      }
    }
  }
  return [null, null];
}

// Case-insensitive, and tolerant of ALL-CAPS transcript headers.
//
// Ordered most-specific first, and that order is load-bearing: the bare
// "UNIVERSITY"/"COLLEGE" keyword pattern matches inside any longer all-caps
// name, so if it runs first it wins on a header like "RIVERBEND STATE
// UNIVERSITY" and yields just "University". The named all-caps pattern has to
// get first refusal; the bare keyword stays only as a last-resort fallback.
const INSTITUTION_RES = [
  /\b((?:[A-Z][\w&.'\-]*\s+){0,4}(?:University|College|Institute of Technology|Polytechnic|Institute)(?:\s+(?:of|at)\s+[A-Z][\w&.'\-]*(?:\s+[A-Z][\w&.'\-]*){0,3})?)\b/,
  /\b([A-Z][A-Z&.'\- ]{3,50}?\s(?:UNIVERSITY|COLLEGE|INSTITUTE))\b/,
  /\b((?:UNIVERSITY|COLLEGE|INSTITUTE)(?:\s+(?:OF|AT)\s+[A-Z][A-Z&.'\- ]{2,40})?)\b/
];
const LABELS = {
  major: /(?:Major|Program of Study|Degree Program)\s*[:\-]\s*(.+)/i,
  minor: /Minor\s*[:\-]\s*(.+)/i,
  concentration: /Concentration\s*[:\-]\s*(.+)/i,
  expectedGrad: /Expected\s+Graduation(?:\s+Date)?\s*[:\-]\s*([A-Za-z0-9 ,/]+)/i,
  gradDate: /(?:Graduation Date|Date Conferred|Conferred)\s*[:\-]\s*([A-Za-z0-9 ,/]+)/i
};
const GPA_RE = /\b(?:cumulative\s+)?(?:gpa|grade\s+point\s+average)\b[^\d]{0,12}(\d\.\d{1,3})\s*(?:\/\s*(\d\.\d{1,2}))?/i;
// Course codes appear anywhere in a transcript, not only under a heading.
const COURSE_RE = /^\s*([A-Z]{1,5}\s?-?\s?\d{3,4}[A-Z]?)\s*[-:.\s]\s*([A-Za-z][A-Za-z0-9 &,/'\-.]{2,70}?)(?=\s{2,}|\s+\d|\s*$)/;

const SECTION_HEADS = {
  coursework: /coursework|courses? taken|relevant courses|course history|academic record/i,
  skills: /technical skills|skills(?:\s*(?:&|and)\s*(?:technologies|tools))?|competencies|proficiencies/i,
  honors: /honors|awards|distinctions|honors\s*(?:&|and)\s*awards/i,
  certifications: /certifications?|licenses?|credentials/i
};
// A line only ends a section if it's an explicit heading — a markdown heading,
// a short trailing-colon label, or one of our known section names. Arbitrary
// short lines are CONTENT (pdf.js yields bullet-less lines like "Python").
const MD_HEAD = /^#{1,6}\s*(.+?)\s*$/;
const COLON_LABEL = /^([A-Za-z][A-Za-z '&\/]{2,40})\s*:\s*$/;
const KNOWN_HEAD = new RegExp('^(?:' + Object.values(SECTION_HEADS).map(r => r.source).join('|') + ')\\s*:?\\s*$', 'i');

function headingTextOf(line) {
  const md = line.match(MD_HEAD);
  if (md) return md[1];
  const colon = line.match(COLON_LABEL);
  if (colon) return colon[1];
  if (KNOWN_HEAD.test(line)) return line.replace(/:\s*$/, '');
  return null;
}

function findSection(lines, headRe) {
  const out = [];
  let capturing = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const head = headingTextOf(line);
    if (head !== null) {
      if (capturing) break;
      if (headRe.test(head)) capturing = true;
      continue;
    }
    if (capturing) out.push(line);
  }
  return out;
}

function labeled(re, text) {
  const m = text.match(re);
  return m ? m[1].trim().replace(/[.,;]$/, '') : null;
}

/** Transcripts often shout; render ALL-CAPS names as Title Case. */
function titleCaseIfShouting(value) {
  if (value !== value.toUpperCase()) return value;
  const small = new Set(['of', 'at', 'and', 'the', 'in']);
  return value.toLowerCase().split(/\s+/)
    .map((w, i) => (i > 0 && small.has(w)) ? w : w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

function listSection(lines, headRe) {
  return findSection(lines, headRe)
    .flatMap(line => line.split(/\s{2,}|[•·|]/))
    .map(s => s.replace(/^[-*•\d.)\s]+/, '').trim())
    .filter(s => s.length > 1 && s.length < 60);
}

/**
 * Scans course codes/titles for skills the title LITERALLY names, and returns
 * them as suggestions for the student to approve. Deliberately conservative:
 * a vague title ("Database Systems", "Intro to Business") yields nothing —
 * only an explicit mention ("Applied SQL", "Python Programming") counts, per
 * the product rule against inferring a technology from a course name.
 */
const SKILL_VOCABULARY = [
  'SQL','Python','Java','JavaScript','TypeScript','C++','C#','R','MATLAB','Excel','VBA',
  'Git','Linux','Docker','Kubernetes','AWS','Azure','REST APIs','HTML','CSS','React',
  'Statistics','Econometrics','Calculus','Linear Algebra','Machine Learning','Data Visualization',
  'Tableau','Power BI','SAS','SPSS','Stata','GIS','CAD','SolidWorks','AutoCAD','Revit',
  'Financial Modeling','Accounting','GAAP','Auditing','Taxation','Supply Chain','Logistics',
  'Marketing','SEO','Public Speaking','Technical Writing','Project Management','Six Sigma',
  'Organic Chemistry','Biochemistry','Microbiology','Anatomy','Physiology','Pharmacology',
  'Circuit Design','Signal Processing','Thermodynamics','Fluid Mechanics','Statics','Dynamics',
  'Figma','UX Research','Adobe Photoshop','Adobe Illustrator','Networking','Cybersecurity'
];

export function deriveCourseworkSkills(coursework) {
  const found = new Map();
  for (const entry of coursework) {
    const title = String(entry);
    for (const skill of SKILL_VOCABULARY) {
      const escaped = skill.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const re = new RegExp('(?<![a-z0-9])' + escaped + '(?![a-z0-9])', 'i');
      if (re.test(title) && !found.has(skill)) found.set(skill, title);
    }
  }
  return [...found.entries()].map(([skill, source]) => ({ skill, source }));
}

export function normalizeAcademicText(text) {
  const lines = text.split('\n');
  const warnings = [];

  let degree = null, degreeLevel = null;
  for (const [re, name, level] of DEGREE_PATTERNS) {
    if (re.test(text)) { degree = name; degreeLevel = level; break; }
  }
  if (!degree) [degree, degreeLevel] = matchDegreeAbbreviation(text);
  const instMatch = INSTITUTION_RES.reduce((found, re) => found || text.match(re), null);
  const institution = instMatch ? titleCaseIfShouting(instMatch[1].trim()) : null;
  const gpaMatch = text.match(GPA_RE);
  const gpa = gpaMatch ? (gpaMatch[2] ? `${gpaMatch[1]}/${gpaMatch[2]}` : gpaMatch[1]) : null;

  // Prefer an explicit coursework section, but fall back to scanning the whole
  // document — most transcripts list courses under term headings, not a
  // "Coursework" heading.
  const sectionLines = findSection(lines, SECTION_HEADS.coursework);
  const courseSource = sectionLines.length ? sectionLines : lines;
  const seen = new Set();
  const coursework = courseSource
    .map(line => line.trim().match(COURSE_RE))
    .filter(Boolean)
    .map(m => `${m[1].replace(/\s+/g, ' ').trim()} — ${m[2].trim().replace(/[.\s]+$/, '')}`)
    .filter(c => { const k = c.toLowerCase(); if (seen.has(k)) return false; seen.add(k); return true; });

  const skills = listSection(lines, SECTION_HEADS.skills);
  const honors = listSection(lines, SECTION_HEADS.honors);
  const certifications = listSection(lines, SECTION_HEADS.certifications);

  if (!institution) warnings.push("We couldn't confidently find your institution — please add it below.");
  if (!degree) warnings.push("We couldn't confidently find your degree — please add it below.");
  if (!coursework.length) warnings.push('No course list was detected — you can add courses below.');
  if (!skills.length) warnings.push("Most transcripts don't list skills. Add yours below so they can be verified against the market.");

  return {
    profile: {
      institution: institution || '',
      degree: degree || '',
      degreeLevel: degreeLevel || '',
      major: labeled(LABELS.major, text) || '',
      minor: labeled(LABELS.minor, text) || '',
      concentration: labeled(LABELS.concentration, text) || '',
      gradDate: labeled(LABELS.expectedGrad, text) || labeled(LABELS.gradDate, text) || '',
      gpa: gpa || '',
      coursework, skills, certifications, honors,
      suggestedSkills: deriveCourseworkSkills(coursework)
    },
    warnings
  };
}

/**
 * Validate the file and return its text layer. Throws PdfValidationError with a
 * student-facing message on any rejection, including the scanned/image-only
 * case so the UI can fall back to manual entry.
 *
 * Split out from `parsePdfFile` because the normal path now stops here: the
 * text goes to the service for Claude-based extraction, and only the offline
 * fallback continues on to `normalizeAcademicText`. Keeping validation on this
 * side means a bad file is rejected before anything is uploaded at all.
 */
export async function extractText(file) {
  await validatePdf(file);
  const text = await extractPdfText(file);
  if (text.trim().length < MIN_EXTRACTED_CHARS) {
    throw new PdfValidationError('This PDF appears to be a scanned image with no selectable text, so we couldn\'t read it. Please enter your academic information manually.');
  }
  return text;
}

/**
 * Full client-side pipeline: validate -> extract text -> normalize. Used when
 * the service is unreachable; `extractText` above is the normal path.
 */
export async function parsePdfFile(file) {
  return normalizeAcademicText(await extractText(file));
}
