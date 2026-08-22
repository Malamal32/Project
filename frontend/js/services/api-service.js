// The frontend's only door to the network. Every call the wizard makes goes
// through this module, and each one is either live against `service/app.py` or
// an explicitly-marked mock standing in for a stage this repo has not built yet.
//
// Live — served by service/app.py (see README "Transcript extraction service"):
//   POST /api/transcript/parse   -> parseTranscript(file)
//   POST /api/linkedin/import    -> importLinkedInExport(file)
//   POST /api/student/profile    -> saveStudentProfile(profile, method)
//   POST /api/resume/generate    -> generateResume({...})
//
// Mocked — the endpoint does not exist yet. These read from the hiring database
// the pipeline builds (CIP majors, O*NET occupations, postings, requirements),
// which lives in Cloudflare D1; the query service in front of it is Phases 4-6
// in PROMPT.md and is not written. The mocks below return the shape that
// service will return, so swapping them is a fetch() and nothing else:
//   GET  /api/roles/search?q=    -> searchRoles(q)
//   POST /api/market/analyze     -> analyzeMarket({...})
// (`course-catalog.js` mocks GET /api/courses/lookup on the same terms.)

// ---------------------------------------------------------------------------
// NOTE: StudentProfile + MarketProfile -> MarketMatch
//
// This mirrors service/market_matching.py exactly (same statuses, same
// conservative rules, same evidence requirement) so the browser can show the
// student their match without a round trip. The server always recomputes it —
// POST /api/resume/generate accepts no MarketMatch — so this copy is for
// display only. The two must not drift: if they disagree, the student sees one
// match and the resume is built against another. Change both or neither.
//
// It never touches a database and never infers a skill from market demand
// alone — demand only decides which skills are worth checking.
// ---------------------------------------------------------------------------

function slugify(name) {
  return (name || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function wordMatch(haystack, needle) {
  if (!haystack) return false;
  const escaped = needle.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp('(?<![a-z0-9])' + escaped + '(?![a-z0-9])');
  return re.test(haystack.toLowerCase());
}

const TOP_N_SKILLS = 10;

/**
 * matchMarket(studentProfile, marketProfile) -> MarketMatch
 * StudentProfile: { skills:[{id,name,aliases?}], certifications:[{id,name}],
 *   coursework:[{id,course_code?,course_name}], experience:[{id,description}],
 *   projects:[{id,technologies,description}] }
 * MarketProfile: { skills:[{id?,name,posting_count,frequency}] }
 */
export function matchMarket(studentProfile, marketProfile) {
  const sp = {
    skills: studentProfile.skills || [], certifications: studentProfile.certifications || [],
    coursework: studentProfile.coursework || [], experience: studentProfile.experience || [], projects: studentProfile.projects || []
  };
  const topSkills = (marketProfile.skills || []).slice(0, TOP_N_SKILLS);

  const checkVerified = (name) => {
    const pool = [...sp.skills, ...sp.certifications];
    const hit = pool.find(item => item.name.toLowerCase() === name.toLowerCase() || (item.aliases || []).some(a => a.toLowerCase() === name.toLowerCase()));
    return hit ? [hit.id] : null;
  };
  const checkCoursework = (name) => {
    const hit = sp.coursework.find(c => wordMatch(c.course_name, name) || wordMatch(c.course_code, name));
    return hit ? [hit.id] : null;
  };
  const checkTransferable = (name) => {
    const expHit = sp.experience.find(e => wordMatch(e.description, name));
    if (expHit) return [expHit.id];
    const projHit = sp.projects.find(p => wordMatch(`${p.technologies || ''} ${p.description || ''}`, name));
    return projHit ? [projHit.id] : null;
  };

  const matches = topSkills.map(sk => {
    const market_skill_id = sk.id || slugify(sk.name);
    let evidence = checkVerified(sk.name), status = 'verified';
    if (!evidence) { evidence = checkCoursework(sk.name); status = 'coursework'; }
    if (!evidence) { evidence = checkTransferable(sk.name); status = 'transferable'; }
    if (!evidence) { evidence = []; status = 'not_verified'; }
    return { market_skill_id, market_skill: sk.name, posting_count: sk.posting_count, frequency: sk.frequency, status, evidence };
  });

  const verified_top_skills = matches.filter(m => m.status !== 'not_verified').length;
  const gaps = matches.filter(m => m.status === 'not_verified').map(m => m.market_skill);
  return { summary: { top_market_skills_considered: topSkills.length, verified_top_skills }, matches, gaps };
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// Stand-in for an O*NET occupation index the real /api/roles/search will query.
const ROLE_INDEX = [
  { display_name: 'Software Developer', onet_soc_code: '15-1252.00', key: 'swe', alt: ['backend developer', 'frontend developer', 'full stack developer', 'software engineer', 'application developer'] },
  { display_name: 'Data Analyst', onet_soc_code: '15-2051.00', key: 'data', alt: ['business intelligence analyst', 'reporting analyst'] },
  { display_name: 'Marketing Specialist', onet_soc_code: '11-2021.00', key: 'marketing', alt: ['digital marketing coordinator', 'brand marketing associate'] },
  { display_name: 'Financial Analyst', onet_soc_code: '13-2051.00', key: 'finance', alt: ['investment analyst', 'equity research analyst'] },
  { display_name: 'UX/Product Designer', onet_soc_code: '15-1255.00', key: 'design', alt: ['ux designer', 'product designer', 'ui designer'] },
  { display_name: 'Accountant', onet_soc_code: '13-2011.00', key: 'accounting', alt: ['staff accountant', 'junior accountant'] },
  { display_name: 'Registered Nurse', onet_soc_code: '29-1141.00', key: 'nursing', alt: ['rn', 'nurse'] },
  { display_name: 'Mechanical Engineer', onet_soc_code: '17-2141.00', key: 'mech_engineering', alt: ['civil engineer', 'manufacturing engineer'] },
  { display_name: 'Human Resources Specialist', onet_soc_code: '13-1071.00', key: 'hr', alt: ['hr coordinator', 'recruiter', 'talent acquisition specialist'] },
  { display_name: 'Sales Representative', onet_soc_code: '41-4012.00', key: 'sales', alt: ['account executive', 'business development representative'] },
  { display_name: 'Elementary/Secondary Teacher', onet_soc_code: '25-2021.00', key: 'teaching', alt: ['teacher', 'instructor'] },
  { display_name: 'Petroleum Engineer', onet_soc_code: '17-2171.00', key: 'petroleum', alt: ['reservoir engineer', 'drilling engineer'] },
  { display_name: 'Electrical Engineer', onet_soc_code: '17-2071.00', key: 'electrical_engineering', alt: ['computer hardware engineer', 'embedded systems engineer'] },
  { display_name: 'Logistician', onet_soc_code: '13-1081.00', key: 'supply_chain', alt: ['supply chain analyst', 'logistics coordinator'] },
  { display_name: 'Construction Manager', onet_soc_code: '11-9021.00', key: 'construction', alt: ['construction project manager'] },
  { display_name: 'Agricultural Manager', onet_soc_code: '11-9013.00', key: 'agriculture', alt: ['farm manager', 'agribusiness specialist'] },
  { display_name: 'Exercise Physiologist', onet_soc_code: '29-1128.00', key: 'kinesiology', alt: ['fitness trainer', 'athletic trainer'] },
  { display_name: 'Police & Detective', onet_soc_code: '33-3051.00', key: 'criminal_justice', alt: ['probation officer', 'correctional officer'] },
  { display_name: 'Public Relations Specialist', onet_soc_code: '27-3031.00', key: 'communications', alt: ['communications coordinator', 'pr associate'] },
  { display_name: 'Medical Scientist', onet_soc_code: '19-1042.00', key: 'biology_prehealth', alt: ['research associate', 'lab technician'] }
];

/**
 * GET /api/roles/search?q=
 *
 * Real O*NET lookup where the service has the index: the pipeline publishes
 * 1,016 occupations and 57,543 alternate titles to D1, and the deployed Worker
 * queries both. The alternate titles are the point — they are why "coder" finds
 * Software Developers.
 *
 * Falls back to the small hardcoded ROLE_INDEX below when the service has no
 * index bound (running locally against SQLite) or is unreachable, so the wizard
 * still autocompletes.
 */
export async function searchRoles(q) {
  const query = (q || '').trim();
  if (!query) return [];

  try {
    const response = await fetch(
      `${TRANSCRIPT_SERVICE_URL}/api/roles/search?q=${encodeURIComponent(query)}`
    );
    if (response.ok) {
      const data = await response.json();
      if (data.results && data.results.length) return data.results;
    }
  } catch (err) {
    console.warn('role search unavailable, using the local index:', err);
  }

  return searchRolesLocally(query);
}

/** The offline/no-index fallback: a short hand-written occupation list. */
function searchRolesLocally(q) {
  const query = q.toLowerCase();
  return ROLE_INDEX
    .filter(r => r.display_name.toLowerCase().includes(query) || r.alt.some(a => a.includes(query)))
    .slice(0, 8)
    .map(r => ({ display_name: r.display_name, onet_soc_code: r.onet_soc_code }));
}

function mapRoleToProfileKey(role) {
  const r = (role || '').toLowerCase();
  const hit = ROLE_INDEX.find(e => e.display_name.toLowerCase() === r || e.alt.includes(r));
  if (hit) return hit.key;
  if (/petroleum|reservoir|drilling|upstream energy/.test(r)) return 'petroleum';
  if (/electrical engineer|computer engineer|embedded/.test(r)) return 'electrical_engineering';
  if (/supply chain|logistics|procurement/.test(r)) return 'supply_chain';
  if (/construction/.test(r)) return 'construction';
  if (/agricultur|agribusiness|ranch|farm/.test(r)) return 'agriculture';
  if (/kinesiology|exercise science|athletic train|personal train/.test(r)) return 'kinesiology';
  if (/criminal justice|law enforcement|corrections|forensic/.test(r)) return 'criminal_justice';
  if (/communications|public relations|\bpr\b|journalism/.test(r)) return 'communications';
  if (/biology|pre-health|pre-med|biomedical science/.test(r)) return 'biology_prehealth';
  if (/nurs|rn\b|clinical|patient care/.test(r)) return 'nursing';
  if (/mechanical|civil|structural|manufactur/.test(r)) return 'mech_engineering';
  if (/accountant|accounting|bookkeep|audit/.test(r)) return 'accounting';
  if (/human resources|^hr\b| hr |recruit/.test(r)) return 'hr';
  if (/sales|account executive|business development/.test(r)) return 'sales';
  if (/teach|educat|instructor/.test(r)) return 'teaching';
  if (/design|ux|ui|product design/.test(r)) return 'design';
  if (/market/.test(r)) return 'marketing';
  if (/financ/.test(r)) return 'finance';
  if (/data analy|analytics/.test(r)) return 'data';
  if (/software|develop|engineer|program/.test(r)) return 'swe';
  return 'general';
}

function onetCodeFor(key) {
  const hit = ROLE_INDEX.find(e => e.key === key);
  return hit ? hit.onet_soc_code : '';
}

// Temporary development mock data standing in for the shared postings DB.
// Skill "frequency" is computed server-side as:
//   (unique relevant postings containing the skill) / (unique relevant postings analyzed)
// — there is no static universal market-value number; these mock profiles
// just pre-bake a plausible result of that computation per occupation.
const MOCK_MARKET_PROFILES = {
  swe: { postings_found: 2140, postings_analyzed: 1876, skills: [
    ['SQL',1388,0.740],['Python',1257,0.670],['Git',1144,0.610],['REST APIs',1032,0.550],['JavaScript',957,0.510],
    ['Linux',844,0.450],['AWS',807,0.430],['Docker',750,0.400],['Agile/Scrum',675,0.360],['Unit Testing',582,0.310]
  ], sample_postings: [
    { title: 'Junior Backend Developer', company: 'Lonestar Digital', location: 'Austin, TX', skills: ['SQL','Python','Git','REST APIs'] },
    { title: 'Software Engineer I', company: 'Meridian Systems', location: 'Remote', skills: ['Python','Git','AWS','Docker'] },
    { title: 'Application Developer, New Grad', company: 'Highline Software', location: 'Dallas, TX', skills: ['SQL','JavaScript','REST APIs','Unit Testing'] },
    { title: 'Platform Engineer I', company: 'Cobalt Cloud', location: 'Austin, TX', skills: ['Linux','AWS','Docker','Agile/Scrum'] }
  ]},
  data: { postings_found: 1860, postings_analyzed: 1612, skills: [
    ['SQL',1306,0.810],['Excel',1161,0.720],['Python',1048,0.650],['Data Visualization',935,0.580],['Tableau',903,0.560],
    ['Power BI',758,0.470],['Statistics',709,0.440],['Communication',629,0.390],['A/B Testing',532,0.330],['R',451,0.280]
  ], sample_postings: [
    { title: 'Junior Data Analyst', company: 'Alamo Insights', location: 'San Antonio, TX', skills: ['SQL','Excel','Data Visualization'] },
    { title: 'Business Intelligence Analyst I', company: 'Northgate Retail', location: 'Remote', skills: ['SQL','Tableau','Power BI'] },
    { title: 'Reporting Analyst, New Grad', company: 'Prairie Analytics', location: 'Houston, TX', skills: ['Excel','Python','Statistics'] }
  ]},
  marketing: { postings_found: 1520, postings_analyzed: 1339, skills: [
    ['Content Creation',830,0.620],['Social Media Strategy',777,0.580],['SEO',737,0.550],['Google Analytics',683,0.510],['Copywriting',629,0.470],
    ['Email Marketing',589,0.440],['Adobe Creative Suite',536,0.400],['Campaign Management',509,0.380],['CRM (HubSpot/Salesforce)',442,0.330],['Project Management',402,0.300]
  ]},
  finance: { postings_found: 1310, postings_analyzed: 1147, skills: [
    ['Excel',906,0.790],['Financial Modeling',700,0.610],['PowerPoint',574,0.500],['Forecasting',539,0.470],['Data Analysis',505,0.440],
    ['SQL',482,0.420],['GAAP',378,0.330],['Valuation',344,0.300],['VBA',287,0.250],['Bloomberg Terminal',252,0.220]
  ]},
  design: { postings_found: 980, postings_analyzed: 861, skills: [
    ['Figma',611,0.710],['User Research',499,0.580],['Wireframing',474,0.550],['Prototyping',448,0.520],['Usability Testing',379,0.440],
    ['Design Systems',344,0.400],['Interaction Design',327,0.380],['Cross-functional Collaboration',301,0.350],['HTML/CSS',284,0.330],['Accessibility',232,0.270]
  ]},
  accounting: { postings_found: 1180, postings_analyzed: 1029, skills: [
    ['Excel',793,0.770],['GAAP',617,0.600],['QuickBooks',545,0.530],['Account Reconciliation',494,0.480],['Accounts Payable/Receivable',463,0.450],
    ['Tax Preparation',401,0.390],['Auditing',370,0.360],['Financial Reporting',350,0.340],['ERP Systems (SAP/Oracle)',298,0.290],['Attention to Detail',267,0.260]
  ]},
  nursing: { postings_found: 1670, postings_analyzed: 1451, skills: [
    ['Patient Care',1263,0.870],['Electronic Health Records (EHR)',972,0.670],['Clinical Assessment',856,0.590],['Medication Administration',798,0.550],['BLS/ACLS Certification',726,0.500],
    ['Care Planning',653,0.450],['IV Therapy',566,0.390],['Infection Control',508,0.350],['Patient Education',464,0.320],['Triage',392,0.270]
  ]},
  mech_engineering: { postings_found: 1040, postings_analyzed: 902, skills: [
    ['CAD (SolidWorks/AutoCAD)',703,0.780],['Finite Element Analysis (FEA)',469,0.520],['GD&T',415,0.460],['MATLAB',379,0.420],['Project Management',343,0.380],
    ['Manufacturing Processes',316,0.350],['Materials Science',289,0.320],['Structural Analysis',262,0.290],['Quality Control',235,0.260],['Technical Documentation',199,0.220]
  ]},
  hr: { postings_found: 890, postings_analyzed: 774, skills: [
    ['Recruiting',565,0.730],['Onboarding',465,0.600],['HRIS (Workday/ADP)',403,0.520],['Employee Relations',372,0.480],['Benefits Administration',325,0.420],
    ['Performance Management',294,0.380],['Employment Law Compliance',263,0.340],['Applicant Tracking Systems',232,0.300],['Communication',209,0.270],['Conflict Resolution',178,0.230]
  ]},
  sales: { postings_found: 1390, postings_analyzed: 1211, skills: [
    ['CRM (Salesforce/HubSpot)',908,0.750],['Prospecting',738,0.610],['Cold Calling',654,0.540],['Negotiation',605,0.500],['Pipeline Management',545,0.450],
    ['Quota Attainment',484,0.400],['Presentation Skills',424,0.350],['Account Management',388,0.320],['Lead Generation',351,0.290],['Communication',315,0.260]
  ]},
  teaching: { postings_found: 760, postings_analyzed: 661, skills: [
    ['Lesson Planning',522,0.790],['Classroom Management',449,0.680],['Curriculum Development',383,0.580],['Differentiated Instruction',317,0.480],['Student Assessment',284,0.430],
    ['IEP/504 Support',251,0.380],['Parent Communication',218,0.330],['Educational Technology',191,0.290],['State Teaching Certification',165,0.250],['Student Engagement',145,0.220]
  ]},
  petroleum: { postings_found: 640, postings_analyzed: 556, skills: [
    ['Reservoir Simulation',400,0.720],['Drilling Operations',361,0.650],['Petrel/Eclipse',300,0.540],['Well Completions',267,0.480],['Production Optimization',233,0.420],
    ['HSE Compliance',200,0.360],['MATLAB',178,0.320],['Geomechanics',156,0.280],['Data Analysis',133,0.240],['Project Management',111,0.200]
  ]},
  electrical_engineering: { postings_found: 970, postings_analyzed: 844, skills: [
    ['Circuit Design',641,0.760],['PCB Layout',464,0.550],['Embedded C/C++',431,0.510],['MATLAB/Simulink',397,0.470],['Signal Processing',355,0.420],
    ['FPGA/VHDL',313,0.370],['Power Systems',279,0.330],['Python',245,0.290],['Testing & Debugging',211,0.250],['Technical Documentation',178,0.210]
  ]},
  supply_chain: { postings_found: 880, postings_analyzed: 766, skills: [
    ['Inventory Management',590,0.770],['ERP Systems (SAP/Oracle)',490,0.640],['Demand Forecasting',421,0.550],['Procurement',383,0.500],['Excel',352,0.460],
    ['Logistics Planning',314,0.410],['Vendor Management',275,0.360],['Lean/Six Sigma',237,0.310],['Data Analysis',199,0.260],['Project Management',168,0.220]
  ]},
  construction: { postings_found: 710, postings_analyzed: 618, skills: [
    ['Project Scheduling',476,0.770],['Blueprint Reading',407,0.660],['Cost Estimating',358,0.580],['Procore/Bluebeam',315,0.510],['OSHA Safety Standards',278,0.450],
    ['Contract Administration',235,0.380],['MS Project',198,0.320],['Quality Control',167,0.270],['Site Supervision',142,0.230],['Budget Management',117,0.190]
  ]},
  agriculture: { postings_found: 520, postings_analyzed: 452, skills: [
    ['Crop Science',335,0.740],['Precision Agriculture Tech',271,0.600],['Farm/Ranch Operations',235,0.520],['Agribusiness Management',199,0.440],['Soil Science',172,0.380],
    ['GIS Mapping',149,0.330],['Excel',127,0.280],['Regulatory Compliance',108,0.240],['Livestock Management',90,0.200],['Communication',72,0.160]
  ]},
  kinesiology: { postings_found: 460, postings_analyzed: 401, skills: [
    ['Exercise Programming',297,0.740],['Fitness Assessment',241,0.600],['CPR/AED Certification',208,0.520],['Injury Prevention',176,0.440],['Anatomy & Physiology',152,0.380],
    ['Client Coaching',132,0.330],['Rehabilitation Protocols',112,0.280],['Nutrition Fundamentals',96,0.240],['Client Record Keeping',80,0.200],['Communication',64,0.160]
  ]},
  criminal_justice: { postings_found: 610, postings_analyzed: 531, skills: [
    ['Report Writing',393,0.740],['Legal Procedures',329,0.620],['Investigative Techniques',281,0.530],['Evidence Handling',244,0.460],['Crisis De-escalation',212,0.400],
    ['Public Safety Protocols',180,0.340],['Records Management',154,0.290],['Communication',132,0.250],['Community Relations',112,0.210],['Conflict Resolution',91,0.170]
  ]},
  communications: { postings_found: 700, postings_analyzed: 609, skills: [
    ['Content Writing',462,0.760],['Media Relations',371,0.610],['Social Media Strategy',335,0.550],['Press Releases',292,0.480],['Brand Messaging',250,0.410],
    ['Adobe Creative Suite',213,0.350],['Public Speaking',183,0.300],['Crisis Communications',152,0.250],['Google Analytics',128,0.210],['Event Planning',104,0.170]
  ]},
  biology_prehealth: { postings_found: 540, postings_analyzed: 470, skills: [
    ['Laboratory Techniques',366,0.780],['Data Collection & Analysis',296,0.630],['Research Methods',254,0.540],['Microscopy',216,0.460],['Patient Interaction',183,0.390],
    ['Regulatory Compliance',155,0.330],['Medical Terminology',132,0.280],['Excel',113,0.240],['Scientific Writing',94,0.200],['Lab Safety Protocols',75,0.160]
  ]},
  general: { postings_found: 1440, postings_analyzed: 1268, skills: [
    ['Communication',735,0.580],['Microsoft Office',697,0.550],['Teamwork',634,0.500],['Problem Solving',596,0.470],['Project Management',558,0.440],
    ['Data Analysis',507,0.400],['Time Management',482,0.380],['Customer Service',419,0.330],['Presentation Skills',380,0.300],['Adaptability',355,0.280]
  ], sample_postings: [
    { title: 'Business Operations Associate', company: 'Sabine Group', location: 'Austin, TX', skills: ['Communication','Microsoft Office','Problem Solving'] },
    { title: 'Coordinator, New Grad Program', company: 'Republic Holdings', location: 'Dallas, TX', skills: ['Teamwork','Project Management','Time Management'] }
  ]}
};

const DATA_AS_OF = '2026-08-19';

/**
 * POST /api/market/analyze
 * request: { role, location, experience_level, remote_preference }
 * response matches the shared market-database service contract:
 * { role:{display_name,onet_soc_code}, postings_found, postings_analyzed, data_as_of, skills[], tools[], credentials[], experience[] }
 */
export async function analyzeMarket({ role, location, experience_level, remote_preference } = {}) {
  await delay(2600);
  const key = mapRoleToProfileKey(role);
  const profile = MOCK_MARKET_PROFILES[key];
  return {
    role: { display_name: role || 'General', onet_soc_code: onetCodeFor(key) },
    postings_found: profile.postings_found,
    postings_analyzed: profile.postings_analyzed,
    data_as_of: DATA_AS_OF,
    skills: profile.skills.map(([name, posting_count, frequency]) => ({ name, posting_count, frequency })),
    tools: [],
    credentials: [],
    experience: [],
    sample_postings: profile.sample_postings || []
  };
}

// Base URL of the transcript extraction service (service/app.py). Empty string
// means same-origin. Set window.TRANSCRIPT_SERVICE_URL to point at it in dev.
const TRANSCRIPT_SERVICE_URL =
  (typeof window !== 'undefined' && window.TRANSCRIPT_SERVICE_URL) || '';

const EMPTY_PROFILE = { institution:'', degree:'', degreeLevel:'', major:'', minor:'', concentration:'', gradDate:'', gpa:'', coursework:[], skills:[], certifications:[], honors:[] };

// ---------------------------------------------------------------------------
// Profile shaping — the evidence-id scheme lives here
//
// The wizard holds the student's data in two places: `academic` (parsed from
// the transcript, then edited) and `activities` (typed in afterwards). The
// service sees a single AcademicProfile, and `service/resume_evidence.py` mints
// evidence ids by POSITION in that profile's `skills` and `certifications`
// lists — `skill_0`, `cert_0`, and so on.
//
// So the order in which those two sources are concatenated *is* the evidence-id
// scheme, and it is defined exactly once: in `collectProfileItems` below.
// `toWireProfile` (what the server indexes) and `toMatchProfile` (what the
// browser's display-only matcher indexes) both read from it, so `skill_3` names
// the same string on both sides. Reorder either list in only one of them and
// every skill claim in the generated resume is dropped as `unknown_evidence_id`
// — the failure is silent and total, which is why this is one function.
//
// Rule: academic first, then activities. Coursework ids are minted here too,
// and the server takes them straight off the request (`run_match`), so those
// agree by construction.

const COURSE_ID_PREFIX = 'course';

const nonEmpty = (list) => (list || []).map(v => String(v == null ? '' : v).trim()).filter(Boolean);

/** Inverse of `courseLabel` below: `"CS 3305 — Data Structures"` -> its parts. */
function splitCourseLabel(label) {
  const parts = String(label).split(' — ');
  return parts.length > 1
    ? { course_code: parts[0].trim(), course_name: parts.slice(1).join(' — ').trim() }
    : { course_code: null, course_name: parts[0].trim() };
}

export function collectProfileItems(academic = {}, activities = {}) {
  return {
    skills: [...nonEmpty(academic.skills), ...nonEmpty(activities.skills)],
    certifications: [...nonEmpty(academic.certifications), ...nonEmpty(activities.certifications)],
    honors: nonEmpty(academic.honors),
    coursework: nonEmpty(academic.coursework).map((label, i) => ({
      id: `${COURSE_ID_PREFIX}_${i}`,
      ...splitCourseLabel(label)
    }))
  };
}

/**
 * UI state -> the snake_case AcademicProfile of service/schemas.py. The
 * outbound half of the adapter whose inbound half is `toUiProfile`.
 *
 * `gradDate` goes out as `expected_graduation_date`; the UI has one field and
 * the validator accepts either (`graduation_date or expected_graduation_date`).
 */
export function toWireProfile(academic = {}, activities = {}) {
  const items = collectProfileItems(academic, activities);
  return {
    institution: academic.institution || null,
    degree: academic.degree || null,
    degree_level: academic.degreeLevel || null,
    major: academic.major || null,
    minor: academic.minor || null,
    concentration: academic.concentration || null,
    expected_graduation_date: academic.gradDate || null,
    gpa: academic.gpa || null,
    coursework: items.coursework.map(c => ({ ...c, student_approved: true })),
    skills: items.skills,
    certifications: items.certifications,
    honors: items.honors
  };
}

/**
 * UI state -> the matcher's StudentProfile. Same ids as `toWireProfile`, plus
 * the experience and projects the student typed in (which the transcript never
 * contains and which are not part of AcademicProfile).
 */
export function toMatchProfile(academic = {}, activities = {}, experience = [], projects = []) {
  const items = collectProfileItems(academic, activities);
  return {
    skills: items.skills.map((name, i) => ({ id: `skill_${i}`, name })),
    certifications: items.certifications.map((name, i) => ({ id: `cert_${i}`, name })),
    coursework: items.coursework,
    experience: (experience || []).map(e => ({ id: e.id, description: e.description || '' })),
    projects: (projects || []).map(p => ({ id: p.id, technologies: p.tech || '', description: p.description || '' }))
  };
}

/**
 * The service returns the snake_case AcademicProfile from service/schemas.py,
 * with `coursework` as objects. The UI (and the browser-side extractor) speak
 * camelCase with `coursework` as display strings. The reverse direction is
 * `toWireProfile` above; between them this module is the only place that knows
 * both shapes, and the service shape is the canonical one.
 */
function toUiProfile(p) {
  const courseLabel = (c) => [c.course_code, c.course_name].filter(Boolean).join(' — ');
  return {
    institution: p.institution || '',
    degree: p.degree || '',
    degreeLevel: p.degree_level || '',
    major: p.major || '',
    minor: p.minor || '',
    concentration: p.concentration || '',
    gradDate: p.expected_graduation_date || p.graduation_date || '',
    gpa: p.gpa || '',
    coursework: (p.coursework || []).map(courseLabel).filter(Boolean),
    skills: p.skills || [],
    certifications: p.certifications || [],
    honors: p.honors || []
  };
}

/**
 * POST /api/transcript/parse-text
 *
 * Extracts the PDF's text in the browser with pdf.js, then sends only that text
 * to the service for Claude-based extraction. The file itself never leaves this
 * machine.
 *
 * That ordering is not just a privacy nicety — it is what lets the service run
 * on Cloudflare Workers at all. Server-side PDF conversion needs MarkItDown,
 * which has native dependencies Pyodide cannot load; the browser already had a
 * pdf.js extractor for its offline fallback, so the split moves the one
 * un-portable step to the side that was already doing it.
 *
 * If the service is unreachable we still have the text, so the flow degrades to
 * the rule-based mirror in academic-extraction.js rather than failing.
 *
 * `onPhase` is called with 'parsing' then 'extracting' as the two halves begin.
 * The second half is a model call and is the slow one; the caller needs to be
 * able to say which one the student is waiting on.
 *
 * Returns { success, academic_profile, warnings, review_required,
 * extraction_method }. Nothing is stored by either path: the student reviews
 * every field, and saving is a separate call to /api/student/profile.
 */
export async function parseTranscript(file, { onPhase = () => {} } = {}) {
  const { extractText, PdfValidationError } = await import('./academic-extraction.js');

  let text;
  try {
    onPhase('parsing');
    text = await extractText(file);
  } catch (err) {
    const message = err instanceof PdfValidationError
      ? err.message
      : "We couldn't read this PDF. Please enter your academic information manually.";
    return { success: false, academic_profile: EMPTY_PROFILE, warnings: [message],
      review_required: true, extraction_method: 'none' };
  }

  try {
    onPhase('extracting');
    const response = await fetch(`${TRANSCRIPT_SERVICE_URL}/api/transcript/parse-text`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text })
    });

    if (response.status === 413) {
      return { success: false, academic_profile: EMPTY_PROFILE, review_required: true, extraction_method: 'none',
        warnings: ['That document is longer than this service accepts.'] };
    }
    if (!response.ok) throw new Error(`transcript service returned ${response.status}`);

    const data = await response.json();
    return {
      success: data.success,
      academic_profile: data.success ? toUiProfile(data.academic_profile) : EMPTY_PROFILE,
      warnings: data.warnings || [],
      review_required: true,
      extraction_method: data.extraction_method || 'none'
    };
  } catch (err) {
    // Network failure, or no service running. Not a reason to block the
    // student — we already have the text, so normalize it here instead.
    console.warn('transcript service unavailable, normalizing in the browser instead:', err);
    return parseTranscriptInBrowser(text);
  }
}

/** Client-side fallback: rule-based normalization of already-extracted text. */
async function parseTranscriptInBrowser(text) {
  const { normalizeAcademicText } = await import('./academic-extraction.js');
  try {
    const { profile, warnings } = normalizeAcademicText(text);
    return { success: true, academic_profile: profile, warnings, review_required: true, extraction_method: 'rules' };
  } catch (err) {
    return { success: false, academic_profile: EMPTY_PROFILE, review_required: true, extraction_method: 'none',
      warnings: ["We couldn't process this document. Please enter your academic information manually."] };
  }
}

/**
 * POST /api/linkedin/import
 *
 * Opens the student's LinkedIn export in the browser and sends only the five
 * CSVs the importer reads. The archive — connections, messages, ad-targeting
 * data, everything else in it — never leaves this machine. See
 * `linkedin-import.js` for the reader and `service/linkedin_import.py` for why
 * this reads an export rather than calling the LinkedIn API.
 *
 * There is no offline fallback, deliberately, and it is not the resume
 * generator's reason. Parsing the CSVs here would be a fourth thing to keep in
 * step with a Python module for no gain: an import that cannot reach the
 * service leaves the student exactly where they were, in front of the form they
 * would have typed into anyway. So this reports the outage and stops.
 *
 * `onPhase` is called with 'reading' then 'importing'. Reading a large archive
 * is the slow half here — the request itself is a few kilobytes of CSV and no
 * model call at all.
 *
 * Returns `{ success, experience, projects, skills, certifications, honors,
 * filesRead, warnings }`, with experience and projects already in the wizard's
 * row shape but *without* ids — `app.js` mints those, because it owns the id
 * space the resume request cites.
 */
export async function importLinkedInExport(file, { onPhase = () => {} } = {}) {
  const { readExport, LinkedInImportError } = await import('./linkedin-import.js');

  let files;
  try {
    onPhase('reading');
    ({ files } = await readExport(file));
  } catch (err) {
    return emptyImport(err instanceof LinkedInImportError
      ? err.message
      : "We couldn't open that file. Please pick the .zip LinkedIn sent you.");
  }

  if (!Object.keys(files).length) {
    return emptyImport(
      "That archive didn't contain any of the files we can read. Make sure you " +
      'requested the full export, not just your profile PDF.'
    );
  }

  try {
    onPhase('importing');
    const response = await fetch(`${TRANSCRIPT_SERVICE_URL}/api/linkedin/import`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ files })
    });
    if (response.status === 413) {
      return emptyImport('That export is larger than this service accepts.');
    }
    if (!response.ok) throw new Error(`import service returned ${response.status}`);
    return toUiImport(await response.json());
  } catch (err) {
    // Reason only — never the archive's contents.
    console.warn('linkedin import service unavailable:', err);
    return emptyImport(
      "We couldn't reach the import service. You can add your experience below instead."
    );
  }
}

function emptyImport(warning) {
  return {
    success: false, experience: [], projects: [], skills: [], certifications: [],
    honors: [], filesRead: [], warnings: [warning]
  };
}

/** The import response in the shapes the wizard's own rows use. The reverse of
 *  what `generateResume` does on the way out, and the same division of labour
 *  as `toUiProfile`: this module is the only place that knows both. */
function toUiImport(data) {
  const dates = (row) => ({
    start: row.started_on || '',
    // A blank Finished On in the archive is LinkedIn's way of saying "current",
    // which is what the form's own "End date (or Present)" label asks for. This
    // is reading the field, not inventing one — and the student can overwrite it
    // on the screen it lands on.
    end: row.finished_on || (row.started_on ? 'Present' : '')
  });

  return {
    success: data.success !== false,
    experience: (data.experience || []).map(e => ({
      employer: e.organization || '',
      role: e.title || '',
      ...dates(e),
      description: e.description || ''
    })),
    projects: (data.projects || []).map(p => ({
      name: p.name || '',
      // The archive has no technologies column. Left blank rather than guessed
      // at from the description — see ImportedProject in service/schemas.py.
      tech: '',
      link: p.url || '',
      description: p.description || ''
    })),
    skills: data.skills || [],
    certifications: data.certifications || [],
    honors: data.honors || [],
    filesRead: data.files_read || [],
    // The server reports skipped files with a reason; the student only needs the
    // ones that were supposed to work and didn't.
    warnings: [
      ...(data.warnings || []),
      ...(data.files_ignored || [])
        .filter(f => f.reason !== 'not one of the files this import reads')
        .map(f => `We couldn't read ${f.name}: it ${f.reason}.`)
    ]
  };
}

/**
 * POST /api/student/profile — store a profile the student has reviewed.
 * The only call in this file that writes anything anywhere.
 */
export async function saveStudentProfile(academicProfile, extractionMethod = 'manual') {
  const response = await fetch(`${TRANSCRIPT_SERVICE_URL}/api/student/profile`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ academic_profile: academicProfile, extraction_method: extractionMethod })
  });
  if (!response.ok) throw new Error(`could not save profile (${response.status})`);
  return response.json();
}

/**
 * POST /api/resume/generate — draft a resume from the reviewed profile plus
 * real hiring demand.
 *
 * Note what is NOT here: a fallback. The service deliberately has none, because
 * falling back would mean generating prose from string templates, which is the
 * thing this stage exists to replace (README, "Differences from the extraction
 * stage"). A template summary is fluent, specific, unverifiable, and lands on
 * the student in an interview. So an outage returns `success: false` and the
 * service's own student-facing warning, and the UI says so.
 *
 * No MarketMatch is sent. The browser mirrors the matcher for display, but the
 * server recomputes the match from the profile below so its evidence ids are
 * the only ids in play.
 *
 * Returns the full GenerateResumeResponse: { success, summary, resume, dropped,
 * gaps, warnings, model_version, variant }. `dropped` is the guardrail made
 * visible — claims the evidence validator deleted, each with a reason.
 */
export async function generateResume({ career, academic, activities, experience, projects, marketProfile, variant }) {
  const body = {
    career: {
      role: (career && career.role) || '',
      level: (career && career.level) || null,
      location: (career && career.location) || null
    },
    academic_profile: toWireProfile(academic, activities),
    experience: (experience || []).map(e => ({
      id: e.id,
      title: e.role || null,
      organization: e.employer || null,
      dates: [e.start, e.end].filter(Boolean).join(' – ') || null,
      description: e.description || ''
    })),
    projects: (projects || []).map(p => ({
      id: p.id,
      name: p.name || null,
      technologies: p.tech || '',
      description: p.description || ''
    })),
    market_profile: { skills: ((marketProfile && marketProfile.skills) || []) },
    variant: variant || 0
  };

  try {
    const response = await fetch(`${TRANSCRIPT_SERVICE_URL}/api/resume/generate`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!response.ok) throw new Error(`resume service returned ${response.status}`);
    return await response.json();
  } catch (err) {
    // Reason only — never the profile.
    console.warn('resume service unavailable:', err);
    return {
      success: false,
      summary: '',
      resume: null,
      dropped: [],
      gaps: [],
      warnings: ["We couldn't reach the resume service. Everything you entered is still here — please try again."],
      variant: variant || 0
    };
  }
}
