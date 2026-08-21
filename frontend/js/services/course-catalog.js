// Course-catalog lookup service.
//
//   GET /api/courses/lookup?institution=&codes=   -> lookupCourses({ institution, codes })
//
// Takes the course codes a student actually took, resolves each against the
// institution's course catalog, and returns the official catalog description
// plus the skills that description explicitly names. Skills come from the
// DESCRIPTION, not from the course title — a catalog description legitimately
// states the technologies a course covers, which is what makes this evidence
// rather than inference.
//
// This is temporary development mock data. Replace the body of lookupCourses()
// with a fetch() to the real catalog service; callers depend only on the
// signature and response shape.

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// Skills a catalog description may explicitly name.
const SKILL_VOCABULARY = [
  'SQL','Python','Java','JavaScript','TypeScript','C++','C#','R','MATLAB','Excel','VBA',
  'Git','Linux','Docker','Kubernetes','AWS','Azure','REST APIs','HTML','CSS','React','Node.js',
  'Statistics','Econometrics','Calculus','Linear Algebra','Machine Learning','Data Visualization',
  'Tableau','Power BI','SAS','SPSS','Stata','GIS','CAD','SolidWorks','AutoCAD','Revit','Simulink',
  'Financial Modeling','Accounting','GAAP','Auditing','Taxation','Valuation','Forecasting',
  'Supply Chain','Logistics','Inventory Management','Procurement','Six Sigma','Project Management',
  'Marketing','SEO','Copywriting','Google Analytics','Public Speaking','Technical Writing',
  'Organic Chemistry','Biochemistry','Microbiology','Anatomy','Physiology','Pharmacology',
  'Laboratory Techniques','Research Methods','Microscopy','Data Collection','Scientific Writing',
  'Circuit Design','PCB Layout','Signal Processing','Embedded C','FPGA','Thermodynamics',
  'Fluid Mechanics','Statics','Dynamics','Finite Element Analysis','GD&T','Materials Science',
  'Figma','Wireframing','Prototyping','User Research','Usability Testing','Accessibility',
  'Networking','Cybersecurity','Agile/Scrum','Unit Testing','Database Design','Data Structures',
  'Algorithms','Operating Systems','Distributed Systems','Curriculum Development','Lesson Planning',
  'Patient Care','Clinical Assessment','Crop Science','Soil Science','Blueprint Reading','Cost Estimating'
];

// Mock catalog: subject-prefix + course-number range -> catalog entry.
// Descriptions are written the way a real catalog states them.
const MOCK_CATALOG = [
  { subjects: ['CS','CSCE','COSC','CPSC'], range: [300, 349], title: 'Data Structures and Algorithms',
    description: 'Design and analysis of fundamental data structures and algorithms. Students implement lists, trees, hash tables, and graph algorithms in Python and Java, analyze asymptotic complexity, and use Git for version control of programming assignments.' },
  { subjects: ['CS','CSCE','COSC','CPSC'], range: [330, 369], title: 'Database Systems',
    description: 'Relational database theory and practice. Covers database design, normalization, and transaction management. Students write substantial SQL queries, design schemas, and build applications against a relational database using Python.' },
  { subjects: ['CS','CSCE','COSC','CPSC'], range: [370, 399], title: 'Software Engineering',
    description: 'Practices of building software in teams. Covers requirements, version control with Git, Agile/Scrum process, code review, unit testing, REST APIs, and continuous integration on Linux environments.' },
  { subjects: ['CS','CSCE','COSC','CPSC'], range: [400, 439], title: 'Operating Systems',
    description: 'Concurrency, memory management, file systems, and scheduling. Programming assignments are written in C++ on Linux.' },
  { subjects: ['CS','CSCE','COSC','CPSC'], range: [440, 499], title: 'Machine Learning',
    description: 'Supervised and unsupervised learning methods. Students implement models in Python, apply statistics to model evaluation, and present results using data visualization.' },
  { subjects: ['M','MATH'], range: [200, 299], title: 'Calculus and Linear Algebra',
    description: 'Multivariable calculus and introductory linear algebra, including matrix operations and applications. Calculus and linear algebra techniques are applied to modeling problems.' },
  { subjects: ['STAT','STA','M','MATH'], range: [300, 399], title: 'Applied Statistics',
    description: 'Probability, estimation, and hypothesis testing. Students perform analyses using R and present findings with data visualization.' },
  { subjects: ['ACCT','ACC','ACCY'], range: [200, 399], title: 'Financial Accounting',
    description: 'Preparation and interpretation of financial statements under GAAP. Coursework includes account reconciliation and extensive use of Excel for statement preparation.' },
  { subjects: ['FIN','FINC'], range: [300, 499], title: 'Corporate Finance',
    description: 'Capital budgeting, valuation, and cost of capital. Students build financial modeling workbooks in Excel and prepare forecasting analyses.' },
  { subjects: ['MKTG','MKT'], range: [300, 499], title: 'Marketing Management',
    description: 'Segmentation, positioning, and campaign planning. Students develop marketing plans, apply SEO and Google Analytics to digital campaigns, and practice copywriting.' },
  { subjects: ['SCMT','SCM','ISQS','MGMT'], range: [300, 499], title: 'Supply Chain Management',
    description: 'Planning and control of supply chain operations, covering inventory management, procurement, logistics, and Six Sigma process improvement, with analysis performed in Excel.' },
  { subjects: ['MEEN','ME','MECH'], range: [200, 349], title: 'Engineering Mechanics',
    description: 'Statics and dynamics of rigid bodies, including force systems and equilibrium analysis. Introduces materials science concepts and technical documentation of results.' },
  { subjects: ['MEEN','ME','MECH'], range: [350, 499], title: 'Thermal and Mechanical Design',
    description: 'Thermodynamics and fluid mechanics applied to design. Students produce CAD models in SolidWorks and perform finite element analysis, applying GD&T to drawings.' },
  { subjects: ['ECEN','EE','ECE'], range: [200, 349], title: 'Circuits and Signals',
    description: 'Linear circuit analysis and continuous-time signals. Covers circuit design fundamentals and signal processing, with simulation in MATLAB and Simulink.' },
  { subjects: ['ECEN','EE','ECE'], range: [350, 499], title: 'Embedded Systems',
    description: 'Microcontroller architecture and real-time programming. Students write embedded C firmware, design PCB layout, and implement logic on FPGA development boards.' },
  { subjects: ['CVEN','CE','CIVE'], range: [200, 499], title: 'Structural Analysis and Construction',
    description: 'Analysis of determinate and indeterminate structures. Includes blueprint reading, cost estimating, and use of AutoCAD and Revit for documentation.' },
  { subjects: ['BIOL','BIO'], range: [200, 399], title: 'Cell and Molecular Biology',
    description: 'Structure and function of cells. Laboratory work develops laboratory techniques, microscopy, data collection, and scientific writing.' },
  { subjects: ['CHEM','CH'], range: [200, 399], title: 'Organic Chemistry',
    description: 'Structure, nomenclature, and reactions of organic compounds. Laboratory emphasizes laboratory techniques and scientific writing.' },
  { subjects: ['NURS','NUR'], range: [200, 499], title: 'Clinical Nursing Practice',
    description: 'Application of the nursing process across the lifespan. Clinical rotations develop patient care, clinical assessment, and patient education competencies.' },
  { subjects: ['KINE','KIN','HLTH'], range: [200, 499], title: 'Exercise Physiology',
    description: 'Physiological responses to exercise. Covers anatomy, physiology, and fitness assessment protocols, with data collection in laboratory settings.' },
  { subjects: ['EDUC','EDCI','TEED'], range: [200, 499], title: 'Instructional Design and Practice',
    description: 'Planning and delivering instruction. Students practice lesson planning, curriculum development, and student assessment in field placements.' },
  { subjects: ['COMM','JOUR','PR'], range: [200, 499], title: 'Strategic Communication',
    description: 'Audience analysis and message strategy. Students practice technical writing, public speaking, and campaign measurement with Google Analytics.' },
  { subjects: ['AGEC','AGRI','ANSC','SCSC'], range: [200, 499], title: 'Agricultural Systems',
    description: 'Production systems and resource management. Covers crop science, soil science, and record keeping, with mapping performed in GIS.' },
  { subjects: ['PETE','PGE'], range: [300, 499], title: 'Reservoir Engineering',
    description: 'Fluid flow in porous media and reservoir performance. Students run reservoir simulation models and analyze production data in MATLAB.' },
  { subjects: ['CRIJ','CJ'], range: [200, 499], title: 'Criminal Investigation',
    description: 'Investigative procedure and case preparation. Emphasizes report writing, evidence handling, and legal procedures.' },
  { subjects: ['MIS','ITEC','INFO','BANA'], range: [300, 499], title: 'Business Analytics',
    description: 'Data-driven decision making. Students query databases with SQL, build dashboards in Tableau and Power BI, and apply statistics to business problems.' },
  { subjects: ['ARTS','DSGN','VIZA'], range: [200, 499], title: 'Interaction Design',
    description: 'Human-centered design process. Students conduct user research, produce wireframing and prototyping work in Figma, and run usability testing with attention to accessibility.' }
];

// Core-curriculum / general-education subjects: excluded from a resume's
// relevant-coursework list because they aren't tied to the major.
const CORE_SUBJECTS = new Set([
  'ENGL','ENG','RHET','WRIT','HIST','HIS','GOV','POLS','PSCI','PHIL','RELS','SOCI','SOC',
  'ANTH','MUSC','MUS','ART','ARTS','THEA','DANC','SPAN','FREN','GERM','CHIN','JAPN','LATN',
  'KINE','PHED','HLTH','UNIV','FYS','SEM','COLL','LIBS','GEOG','ASTR','ENVS','NUTR','SPCH'
]);

// Subjects that belong to each major family. Used to keep courses that are
// genuinely part of the student's program of study.
const MAJOR_SUBJECTS = [
  { match: /computer science|software|computing|informatics/i, subjects: ['CS','CSCE','COSC','CPSC','ECEN','EE','ECE','MATH','M','STAT','MIS','ITEC','INFO'] },
  { match: /data science|analytics|statistics/i, subjects: ['STAT','STA','DATA','CS','CSCE','MATH','M','BANA','MIS','ISQS','INFO'] },
  { match: /mechanical engineer/i, subjects: ['MEEN','ME','MECH','MATH','M','PHYS','MSEN','AERO'] },
  { match: /civil engineer|construction/i, subjects: ['CVEN','CE','CIVE','COSC','ARCH','MATH','M','MEEN'] },
  { match: /electrical|computer engineer/i, subjects: ['ECEN','EE','ECE','CS','CSCE','MATH','M','PHYS'] },
  { match: /petroleum|energy engineer/i, subjects: ['PETE','PGE','GEOL','MATH','M','MEEN','CHEN'] },
  { match: /chemical engineer/i, subjects: ['CHEN','CHE','CHEM','CH','MATH','M'] },
  { match: /industrial|systems engineer/i, subjects: ['ISEN','IE','MEEN','STAT','MATH','M'] },
  { match: /accounting/i, subjects: ['ACCT','ACC','ACCY','FIN','FINC','BUSI','MGMT','ECON'] },
  { match: /finance/i, subjects: ['FIN','FINC','ACCT','ACC','ECON','BANA','MATH','M'] },
  { match: /marketing/i, subjects: ['MKTG','MKT','BUSI','MGMT','COMM','BANA'] },
  { match: /supply chain|logistics|operations/i, subjects: ['SCMT','SCM','ISQS','MGMT','BUSI','STAT'] },
  { match: /management|business administration|entrepreneur/i, subjects: ['MGMT','BUSI','BUAD','ACCT','FIN','MKTG','SCMT','ECON'] },
  { match: /economics/i, subjects: ['ECON','ECO','MATH','M','STAT','FIN'] },
  { match: /management information|information systems/i, subjects: ['MIS','ITEC','INFO','ISQS','CS','CSCE','BANA'] },
  { match: /nursing/i, subjects: ['NURS','NUR','BIOL','BIO','CHEM','CH','ANAT','PHYS'] },
  { match: /biology|pre-?med|pre-?health|biomedical/i, subjects: ['BIOL','BIO','CHEM','CH','BICH','GENE','MICR','PHYS','MATH','M'] },
  { match: /chemistry/i, subjects: ['CHEM','CH','BICH','PHYS','MATH','M'] },
  { match: /kinesiology|exercise science|athletic training/i, subjects: ['KINE','KIN','HLTH','BIOL','BIO','NUTR'] },
  { match: /psychology/i, subjects: ['PSYC','PSY','STAT','BIOL','SOCI'] },
  { match: /education|teaching/i, subjects: ['EDUC','EDCI','TEED','EPSY','READ','SPED'] },
  { match: /communication|journalism|public relations/i, subjects: ['COMM','JOUR','PR','ADV','MDIA'] },
  { match: /criminal justice|criminology|forensic/i, subjects: ['CRIJ','CJ','SOCI','PSYC','FORS'] },
  { match: /agricultur|animal science|agribusiness|soil|crop/i, subjects: ['AGEC','AGRI','ANSC','SCSC','POSC','HORT','RENR'] },
  { match: /architecture/i, subjects: ['ARCH','ARCC','ENDS','COSC','ARTS'] },
  { match: /design|graphic|visualization/i, subjects: ['ARTS','DSGN','VIZA','ARCH','COMM'] },
  { match: /mathematics/i, subjects: ['MATH','M','STAT','CS','CSCE'] },
  { match: /physics/i, subjects: ['PHYS','MATH','M','ASTR'] }
];

/**
 * Filters a raw transcript course list down to the courses a resume should
 * carry: those in the student's major's subject family, plus any upper-division
 * course (300+) that isn't core curriculum. General-education / core courses
 * are dropped. Returns { relevant, excluded } so the student can see and
 * override what was filtered out.
 */
export function filterRelevantCourses({ major, degree, courses } = {}) {
  const subjectField = `${major || ''} ${degree || ''}`;
  const family = MAJOR_SUBJECTS.find(m => m.match.test(subjectField));
  const majorSubjects = new Set(family ? family.subjects : []);

  const relevant = [];
  const excluded = [];
  for (const raw of (courses || [])) {
    const parsed = parseCourseCode(raw);
    if (!parsed) { excluded.push(String(raw)); continue; }
    const isCore = CORE_SUBJECTS.has(parsed.subject);
    const inMajor = majorSubjects.has(parsed.subject);
    const upperDivision = parsed.number >= 300;
    // Keep if it belongs to the major family, or is an upper-division course
    // outside the core curriculum. Everything else is general education.
    if (inMajor || (upperDivision && !isCore)) relevant.push(String(raw));
    else excluded.push(String(raw));
  }
  return { relevant, excluded };
}

function parseCourseCode(raw) {
  const m = String(raw).match(/([A-Z]{1,5})\s?-?\s?(\d{3,4})/i);
  if (!m) return null;
  return { subject: m[1].toUpperCase(), number: parseInt(m[2], 10), code: `${m[1].toUpperCase()} ${m[2]}` };
}

function findCatalogEntry(parsed) {
  return MOCK_CATALOG.find(entry =>
    entry.subjects.includes(parsed.subject) &&
    parsed.number >= entry.range[0] && parsed.number <= entry.range[1]
  ) || null;
}

/** Skills the description text explicitly names. Literal matches only. */
export function skillsFromDescription(description) {
  const out = [];
  for (const skill of SKILL_VOCABULARY) {
    const escaped = skill.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp('(?<![a-z0-9])' + escaped + '(?![a-z0-9])', 'i');
    if (re.test(description)) out.push(skill);
  }
  return out;
}

/**
 * GET /api/courses/lookup
 * request:  { institution, codes: ["CS 314", "MATH 251", ...] }
 * response: { institution, resolved: [{ code, catalog_title, description,
 *             extracted_skills: [] }], unresolved: ["..."] }
 */
export async function lookupCourses({ institution, codes } = {}) {
  await delay(900);
  const resolved = [];
  const unresolved = [];

  for (const raw of (codes || [])) {
    const parsed = parseCourseCode(raw);
    const entry = parsed ? findCatalogEntry(parsed) : null;
    if (!entry) { unresolved.push(String(raw)); continue; }
    resolved.push({
      code: parsed.code,
      catalog_title: entry.title,
      description: entry.description,
      extracted_skills: skillsFromDescription(entry.description)
    });
  }

  return { institution: institution || '', resolved, unresolved };
}
