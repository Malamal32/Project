// Pathfinder — the Career Pathfinder wizard.
//
// Port of the design prototype's DCLogic component (Career Pathfinder.dc.html)
// to an Alpine component. The prototype's `state` becomes this component's own
// fields, its `renderVals()` values become getters, and its `<sc-if>`/`<sc-for>`
// tags become x-show/x-for in index.html.
//
// All wizard state lives here, in the tab, and is not persisted across a
// reload. It is not, however, private to the tab, and the landing screen says
// so: five steps leave the browser, each through js/services/api-service.js.
//
//   - uploading a transcript reads the PDF here and POSTs only its text to the
//     extraction service, which sends that text to the Anthropic API (the file
//     itself never leaves the tab; falls back to in-browser rule-based parsing
//     of the same text when the service is unreachable);
//   - importing a LinkedIn export opens the .zip here and POSTs only the five
//     CSVs the importer reads — no model call, and the archive's connections
//     and messages never leave the tab;
//   - continuing past the review step saves the reviewed profile to the
//     database — the only write in the whole flow;
//   - generating a resume sends that profile plus the experience and projects
//     (typed or imported) to the service, which sends them to the Anthropic API;
//   - polishing one description sends that description and the fields on the
//     same card (role and employer, or project name and technology) to the
//     service, which sends them to the Anthropic API. Only on a click, only the
//     one card, and never automatically.
//
// Nothing else does. If you add a call, add it to that list and to the
// disclosure copy in index.html.

import * as api from './services/api-service.js';
import * as catalog from './services/course-catalog.js';

const TOTAL_STEPS = 10;

const ANALYSIS_MESSAGES = [
  'Scanning current job postings for this role…',
  'Identifying the most frequently requested skills…',
  'Comparing employer demand to your verified background…',
  'Finalizing your Market Match…'
];

// The market analysis is fast enough to feel like nothing happened. Hold the
// spinner long enough for the staged messages to actually be read.
const MESSAGE_INTERVAL_MS = 750;

// Mirrors RESUME_MAX_VARIANT in service/config.py. "Regenerate wording" is a
// variant integer rather than a temperature — sampling parameters are rejected
// on this model — and the server clamps it to the same bound.
const RESUME_MAX_VARIANT = 9;

// service/resume_evidence.py drop reasons, in student-facing words. Keys must
// stay in step with the constants at the top of that module.
const DROP_REASONS = {
  no_evidence: 'No supporting item in your profile',
  unknown_evidence_id: 'Cited something not in your profile',
  evidence_out_of_scope: 'Cited the wrong item',
  not_verified_skill: 'Names a skill you have not listed',
  gap_skill_mentioned: 'Mentions a skill employers want but you have not verified',
  unsupported_number: 'States a figure your profile does not support',
  verbatim_mismatch: 'Reworded a field that must be quoted exactly',
  duplicate: 'Repeats another line'
};

function delayMin(ms) { return new Promise(r => setTimeout(r, ms)); }

let _id = 0;
function nid() { return 'id' + (++_id) + '_' + Math.random().toString(36).slice(2, 7); }

// Per-row state for the Polish button. Seeded on every experience and project
// row from one place — typed rows and LinkedIn-imported rows alike — so the row
// shape is defined once and the template never binds to an undefined field.
// `descriptionBefore` is null when there is nothing to undo, which is also what
// the Undo button keys off.
function polishFields() {
  return { polishStatus: 'idle', polishWarning: '', descriptionBefore: null };
}

function formatDataDate(iso) {
  try {
    return new Date(iso + 'T00:00:00').toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
  } catch (e) {
    return iso;
  }
}

const normCode = v => String(v).toUpperCase().replace(/[^A-Z0-9]/g, '');

const LEVEL_MAP = { 'Internship': 'internship', 'Entry level': 'entry_level', 'Early career': 'early_career' };
const REMOTE_MAP = { 'Remote only': 'remote', 'Hybrid': 'hybrid', 'On-site': 'onsite', 'Open to any': 'any' };

function freshState() {
  return {
    step: 0,
    career: { role: '', industry: '', location: '', level: 'Entry level', remote: 'Open to any' },
    contact: { name: '', email: '', phone: '', city: '', linkedin: '', portfolio: '' },
    uploadStatus: 'idle',
    uploadWarnings: [],
    catalogStatus: 'idle',
    catalogResults: [],
    catalogUnresolved: [],
    excludedCourses: [],
    academic: {
      institution: '', degree: '', degreeLevel: '', major: '', minor: '', concentration: '',
      gradDate: '', gpa: '', coursework: [], skills: [], certifications: [], honors: [],
      suggestedSkills: []
    },
    academicDrafts: { coursework: '', skills: '', certifications: '', honors: '' },
    experience: [],
    projects: [],
    linkedinStatus: 'idle',
    linkedinWarnings: [],
    // What the last import actually drew from, so the student can see whether
    // the thin result was the archive or us. Null until one has run.
    linkedinSummary: null,
    activities: { skills: [], certifications: [], extracurriculars: [], leadership: [], volunteering: [] },
    activityDrafts: { skills: '', certifications: '', extracurriculars: '', leadership: '', volunteering: '' },
    analysisMessage: 'Scanning current job postings…',
    summaryVariant: 0,
    marketProfile: null,
    marketMatch: null,
    resumeSummaryText: '',
    resumeStatus: 'idle',
    resumeWarnings: [],
    resumeDropped: [],
    // Evidence ids of the courses the drafter chose to surface, in its order.
    // Empty means "no selection came back" — see `courseworkTitles`.
    resumeCourseIds: [],
    // The drafted one-sentence description of that selection. Empty falls back
    // to the titles themselves — see `refreshResumeSummary`.
    resumeCourseworkText: '',
    // Which stage produced the academic fields, carried from the parse response
    // through to the save so the stored row records real provenance. "manual"
    // is legitimate — a student may type in everything themselves.
    extractionMethod: 'manual',
    profileSaved: false,
    roleSuggestions: []
  };
}

export function pathfinder() {
  return {
    ...freshState(),

    // Not part of the resettable wizard state.
    _analysisRunning: false,
    _roleQueryToken: 0,
    _uploadToken: 0,
    _linkedinToken: 0,

    // ---- static field descriptors -------------------------------------
    // The prototype rebuilt these arrays (with a closure per field) on every
    // render. They're constant, so they're declared once and the markup binds
    // straight through to state with x-model.

    careerFields: [
      { key: 'industry', label: 'Target industry', placeholder: 'e.g. Technology' },
      { key: 'location', label: 'Preferred location', placeholder: 'e.g. Austin, TX' }
    ],
    levelOptions: ['Internship', 'Entry level', 'Early career'],
    remoteOptions: ['Remote only', 'Hybrid', 'On-site', 'Open to any'],
    contactFields: [
      { key: 'name', label: 'Full name', placeholder: 'Jordan Rivera', span: 'grid-column:1/3' },
      { key: 'email', label: 'Email', placeholder: 'jordan@school.edu', span: '' },
      { key: 'phone', label: 'Phone', placeholder: '(555) 123-4567', span: '' },
      { key: 'city', label: 'City, State', placeholder: 'Austin, TX', span: '' },
      { key: 'linkedin', label: 'LinkedIn (optional)', placeholder: 'linkedin.com/in/jordan', span: '' },
      { key: 'portfolio', label: 'Portfolio / GitHub (optional)', placeholder: 'github.com/jordan', span: 'grid-column:1/3' }
    ],
    academicFields: [
      { key: 'institution', label: 'Institution', span: 'grid-column:1/3' },
      { key: 'degree', label: 'Degree', span: '' },
      { key: 'degreeLevel', label: 'Degree level', span: '' },
      { key: 'major', label: 'Major', span: '' },
      { key: 'minor', label: 'Minor (optional)', span: '' },
      { key: 'gradDate', label: 'Expected graduation', span: '' },
      { key: 'gpa', label: 'GPA (only if found)', span: '' }
    ],
    experienceFields: [
      { key: 'employer', label: 'Employer / organization' },
      { key: 'role', label: 'Role / title' },
      { key: 'start', label: 'Start date' },
      { key: 'end', label: 'End date (or Present)' }
    ],
    projectFields: [
      { key: 'name', label: 'Project name', span: '' },
      { key: 'tech', label: 'Technology', span: '' },
      { key: 'link', label: 'Link (optional)', span: 'grid-column:1/3' }
    ],

    // Inline styles the prototype computed once and reused across screens.
    stepHeadStyle: 'font-family:var(--font-heading);font-size:30px;margin:0 0 var(--space-2)',
    stepSubStyle: 'opacity:0.7;font-size:15px;margin:0 0 var(--space-6);max-width:56ch',
    resumeHeadStyle: 'font-size:12px;letter-spacing:0.1em;text-transform:uppercase;font-weight:700;border-bottom:1px solid #ccc;padding-bottom:4px',

    // ---- navigation ----------------------------------------------------

    get progressStep() { return Math.max(1, Math.min(this.step, TOTAL_STEPS)); },
    get showProgress() { return this.step >= 1 && this.step <= 9; },
    get stepLabel() { return `Step ${this.progressStep} of ${TOTAL_STEPS}`; },
    get progressBarStyle() {
      return `height:100%;background:var(--color-accent);transition:width 0.3s;width:${this.progressStep / TOTAL_STEPS * 100}%`;
    },

    get showFooterNav() { return (this.step >= 1 && this.step <= 7) || this.step === 9; },
    get showBack() { return this.step >= 1 && this.step !== 9; },
    get nextLabel() {
      if (this.step === 7) return 'Analyze the market';
      if (this.step === 9) return 'Continue to my resume';
      return 'Continue';
    },
    /** Steps 1-4 each have a minimum the student must supply before moving on.
     *  Every other step is optional and never blocks. */
    get nextDisabled() {
      const gates = {
        1: !!this.career.role,
        2: !!(this.contact.name && this.contact.email),
        3: ['done', 'failed', 'manual'].includes(this.uploadStatus),
        4: !!(this.academic.institution && this.academic.major)
      };
      return this.step in gates ? !gates[this.step] : false;
    },

    goStart() { this.step = 1; },
    goBack() { this.step = this.step - 1; },
    goEdit() { this.step = 7; },
    goNext() {
      if (this.step === 9) { this.step = 10; return; }
      const next = this.step === 7 ? 8 : this.step + 1;
      this.step = next;
      if (next === 8) {
        // Step 7 is the last review screen, so this transition is the moment
        // the student has approved everything — which is exactly the contract
        // POST /api/student/profile describes. Fire and forget: the analysis
        // must not wait on a write, and a failed write must not strand the
        // student mid-flow.
        this.saveProfile();
        this.runAnalysis();
      }
    },

    /** The only write in the whole flow. Failure is logged and swallowed —
     *  losing the stored copy costs us a row, blocking the wizard costs the
     *  student everything they typed. */
    async saveProfile() {
      if (this.profileSaved) return;
      try {
        await api.saveStudentProfile(
          api.toWireProfile(this.academic, this.activities),
          this.extractionMethod
        );
        this.profileSaved = true;
      } catch (err) {
        console.warn('could not save profile:', err);
      }
    },
    clearSession() { Object.assign(this, freshState()); },

    // ---- career target -------------------------------------------------

    /** Autocomplete against the occupation index. Results are dropped if the
     *  student kept typing while the lookup was in flight, so a slow response
     *  can't overwrite suggestions for a newer query. */
    async onRoleChange(value) {
      this.career.role = value;
      const token = ++this._roleQueryToken;
      const results = await api.searchRoles(value);
      if (token === this._roleQueryToken) this.roleSuggestions = results;
    },
    pickRole(suggestion) {
      this.career.role = suggestion.display_name;
      this.roleSuggestions = [];
    },
    get hasRoleSuggestions() { return this.roleSuggestions.length > 0; },

    // ---- transcript upload ---------------------------------------------

    // The two busy states are the two things that actually happen, in order:
    // the browser reads the PDF, then the text (only the text) goes to the
    // service. Naming them separately is not decoration — extraction can take
    // the better part of a minute, and a student staring at one frozen label
    // has no way to tell a slow model from a hung page.
    get uploadPrompt() {
      return {
        parsing: 'Reading your document…',
        extracting: 'Pulling out your details…',
        done: 'Document read',
        failed: "We couldn't read that PDF",
        manual: 'Entering details manually'
      }[this.uploadStatus] || 'Drop your PDF here or click to upload';
    },
    get uploadSub() {
      return {
        parsing: 'Your browser is reading the PDF — the file itself is never uploaded',
        extracting: 'Only the text was sent. This can take up to a minute',
        done: 'Continue to review what we found',
        failed: 'Try another file, or continue and enter your details manually',
        manual: 'Continue to fill in your academic information'
      }[this.uploadStatus] || 'Transcript, unofficial transcript, or degree audit';
    },
    get uploadBusy() { return this.uploadStatus === 'parsing' || this.uploadStatus === 'extracting'; },
    get hasUploadWarnings() { return (this.uploadWarnings || []).length > 0; },

    async handleUpload(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      const token = ++this._uploadToken;
      this.uploadStatus = 'parsing';
      this.uploadWarnings = [];
      const res = await api.parseTranscript(file, {
        onPhase: phase => { if (token === this._uploadToken) this.uploadStatus = phase; }
      });
      // A student who gave up waiting and chose to type their details, or who
      // picked a different file, has moved on. Landing this result on top of
      // that would overwrite what they typed with fields they never saw, and
      // mislabel them as extracted — so a superseded parse is dropped whole.
      if (token !== this._uploadToken) return;
      this.academic = { ...this.academic, ...res.academic_profile };
      this.uploadWarnings = res.warnings || [];
      this.uploadStatus = res.success ? 'done' : 'failed';
      // "none" means nothing was extracted, so whatever ends up in the profile
      // was typed by the student.
      this.extractionMethod = res.success && res.extraction_method !== 'none'
        ? res.extraction_method
        : 'manual';
      if (res.success) await this.processTranscriptCourses();
    },
    skipUpload() {
      this._uploadToken++;   // abandons a parse still in flight — see handleUpload
      this.uploadStatus = 'manual';
      this.uploadWarnings = [];
      this.extractionMethod = 'manual';
      this.step = 4;         // the screen that has the fields to type into
    },

    // ---- coursework and catalog ----------------------------------------

    /** After a successful parse: drop core/gen-ed courses, look the rest up in
     *  the catalog, and adopt the skills those official descriptions name. */
    async processTranscriptCourses() {
      const a = this.academic;
      const { relevant, excluded } = catalog.filterRelevantCourses({
        major: a.major, degree: a.degree, courses: a.coursework || []
      });
      this.academic.coursework = relevant;
      this.excludedCourses = excluded;
      this.catalogStatus = 'loading';

      const res = await catalog.lookupCourses({ institution: a.institution, codes: relevant });
      const derived = [...new Set(res.resolved.flatMap(c => c.extracted_skills))];
      const have = new Set((this.academic.skills || []).map(x => x.toLowerCase()));

      this.catalogResults = res.resolved;
      this.catalogUnresolved = res.unresolved;
      this.catalogStatus = 'done';
      this.academic.skills = [...(this.academic.skills || []), ...derived.filter(sk => !have.has(sk.toLowerCase()))];
    },

    async lookupCourseCatalog() {
      const codes = this.academic.coursework || [];
      if (!codes.length) return;
      this.catalogStatus = 'loading';
      const res = await catalog.lookupCourses({ institution: this.academic.institution, codes });
      this.catalogResults = res.resolved;
      this.catalogUnresolved = res.unresolved;
      this.catalogStatus = 'done';
    },

    get canLookupCatalog() { return (this.academic.coursework || []).length > 0; },
    get catalogLoading() { return this.catalogStatus === 'loading'; },
    get catalogLookupLabel() {
      return this.catalogStatus === 'loading' ? 'Looking up your courses…' : 'Look up my course descriptions';
    },
    get hasCatalogResults() { return this.catalogResults.length > 0; },
    get hasUnresolvedCourses() { return this.catalogUnresolved.length > 0; },
    get unresolvedCoursesLabel() { return this.catalogUnresolved.join(', '); },
    get hasExcludedCourses() { return (this.excludedCourses || []).length > 0; },
    get excludedCoursesLabel() { return (this.excludedCourses || []).join(', '); },
    restoreExcluded() {
      this.academic.coursework = [...(this.academic.coursework || []), ...this.excludedCourses];
      this.excludedCourses = [];
    },

    /** Everything the student has actually claimed, for deciding whether a
     *  suggested skill chip still needs offering. */
    get claimedSkills() {
      return new Set([...(this.academic.skills || []), ...(this.activities.skills || [])].map(x => x.toLowerCase()));
    },

    get catalogCards() {
      const claimed = this.claimedSkills;
      return this.catalogResults.map(course => ({
        code: course.code,
        title: course.catalog_title,
        description: course.description,
        skillChips: course.extracted_skills.map(skill => {
          const held = claimed.has(skill.toLowerCase());
          return {
            skill,
            label: held ? `✓ ${skill}` : `+ ${skill}`,
            held,
            cls: held ? 'tag' : 'tag tag-outline',
            style: held
              ? 'background:var(--color-accent-100);color:var(--color-accent-700);cursor:default;border:none;font-family:inherit;font-size:inherit'
              : 'cursor:pointer;font-family:inherit;font-size:inherit'
          };
        })
      }));
    },
    get allCatalogSkills() {
      const claimed = this.claimedSkills;
      return [...new Set(this.catalogResults.flatMap(c => c.extracted_skills))]
        .filter(sk => !claimed.has(sk.toLowerCase()));
    },
    get hasAllCatalogSkills() { return this.allCatalogSkills.length > 0; },
    addAllCatalogSkills() {
      this.academic.skills = [...(this.academic.skills || []), ...this.allCatalogSkills];
    },
    addSkill(skill) {
      this.academic.skills = [...(this.academic.skills || []), skill];
    },

    /** Skills the parser found written literally in a course title. Offered,
     *  never auto-added — the student decides what they'd genuinely claim. */
    get suggestedSkills() {
      const claimed = this.claimedSkills;
      return (this.academic.suggestedSkills || [])
        .filter(sug => !claimed.has(sug.skill.toLowerCase()))
        .map(sug => ({ skill: sug.skill, source: `From: ${sug.source}` }));
    },
    get hasSuggestedSkills() { return this.suggestedSkills.length > 0; },

    // ---- generic tag-list editors --------------------------------------
    // Nine lists on three screens share this add/remove behavior. The spec
    // carries the state path so the markup can bind generically.

    listSpec(objKey, draftsKey, key, label, placeholder) {
      return { objKey, draftsKey, key, label, placeholder, items: this[objKey][key] || [] };
    },
    addListItem(spec) {
      const value = (this[spec.draftsKey][spec.key] || '').trim();
      if (!value) return;
      this[spec.objKey][spec.key] = [...(this[spec.objKey][spec.key] || []), value];
      this[spec.draftsKey][spec.key] = '';
    },
    removeListItem(spec, idx) {
      this[spec.objKey][spec.key] = this[spec.objKey][spec.key].filter((_, i) => i !== idx);
    },

    get courseworkList() {
      return this.listSpec('academic', 'academicDrafts', 'coursework', "Courses you've taken", 'e.g. CSCE 314');
    },
    get academicLists() {
      return [
        this.listSpec('academic', 'academicDrafts', 'skills', 'Your skills', 'Add a skill'),
        this.listSpec('academic', 'academicDrafts', 'certifications', 'Certifications', 'Add a certification'),
        this.listSpec('academic', 'academicDrafts', 'honors', 'Honors', 'Add an honor')
      ];
    },
    get activityLists() {
      return [
        this.listSpec('activities', 'activityDrafts', 'skills', 'Skills', 'Add a skill'),
        this.listSpec('activities', 'activityDrafts', 'certifications', 'Certifications', 'Add a certification'),
        this.listSpec('activities', 'activityDrafts', 'extracurriculars', 'Extracurriculars', 'Add an activity'),
        this.listSpec('activities', 'activityDrafts', 'leadership', 'Leadership', 'Add a leadership role'),
        this.listSpec('activities', 'activityDrafts', 'volunteering', 'Volunteering', 'Add volunteer work')
      ];
    },

    // ---- experience and projects ---------------------------------------

    addExperience() {
      this.experience.push({ id: nid(), employer: '', role: '', start: '', end: '', description: '', ...polishFields() });
    },
    removeExperience(idx) { this.experience.splice(idx, 1); },
    addProject() {
      this.projects.push({ id: nid(), name: '', tech: '', link: '', description: '', ...polishFields() });
    },
    removeProject(idx) { this.projects.splice(idx, 1); },

    /** Rewrite one row's description into resume lines, in place.
     *
     *  Takes the row object rather than an index, like the bindings in
     *  index.html and for the same reason: a row can be removed while this is
     *  in flight, and an index into the shortened array would write to the
     *  wrong card.
     *
     *  `description` is assigned in exactly one place — inside the success
     *  branch — which is what preserves the student's own words on every
     *  failure path without a single line of restore logic. There is
     *  deliberately no offline fallback; see `polishDescription` in
     *  api-service.js for why a local one would be worse than none. */
    async polishDescription(row, kind) {
      if (!(row.description || '').trim() || row.polishStatus === 'polishing') return;
      row.polishStatus = 'polishing';
      row.polishWarning = '';

      const res = await api.polishDescription({ kind, item: row });

      if (res.success && res.description) {
        row.descriptionBefore = row.description;
        row.description = res.description;
        row.polishStatus = 'done';
      } else {
        row.polishStatus = 'failed';
      }
      // Shown on success too: a partial result carries the reason a line was
      // dropped, and that is the guardrail made visible rather than an error.
      row.polishWarning = (res.warnings || [])[0] || '';
    },

    /** One level, and that is enough: polishing again after an undo re-captures.
     *  Stays available after the student edits the polished text, because
     *  "give me back what I wrote" is still a meaningful thing to ask then. */
    undoPolish(row) {
      if (row.descriptionBefore === null) return;
      row.description = row.descriptionBefore;
      row.descriptionBefore = null;
      row.polishStatus = 'idle';
      row.polishWarning = '';
    },

    // ---- LinkedIn export import ----------------------------------------
    // A transcript has no work history in it, so before this the only source
    // for the experience section was the form below. The import fills the same
    // rows the form does and is editable in exactly the same way — it is a
    // faster way to type, not a separate, trusted channel.

    get linkedinPrompt() {
      return {
        reading: 'Opening your archive…',
        importing: 'Reading your history…',
        done: 'Import complete',
        failed: "We couldn't read that export"
      }[this.linkedinStatus] || 'Import from a LinkedIn export';
    },
    get linkedinSub() {
      return {
        reading: 'Your browser is opening the .zip — the archive is never uploaded',
        importing: 'Only your positions, projects, skills and honors were sent',
        done: this.linkedinSummaryLabel,
        failed: 'Add your experience below instead'
      }[this.linkedinStatus] || 'Settings → Get a copy of your data → the .zip LinkedIn emails you';
    },
    get linkedinBusy() { return this.linkedinStatus === 'reading' || this.linkedinStatus === 'importing'; },
    get hasLinkedinWarnings() { return (this.linkedinWarnings || []).length > 0; },
    get linkedinSummaryLabel() {
      const s = this.linkedinSummary;
      if (!s) return '';
      const parts = [
        [s.experience, 'position'], [s.projects, 'project'],
        [s.skills, 'skill'], [s.certifications, 'certification'], [s.honors, 'honor']
      ].filter(([n]) => n > 0).map(([n, word]) => `${n} ${word}${n === 1 ? '' : 's'}`);
      return parts.length ? `Added ${parts.join(', ')}` : 'Nothing new to add — you already had it all';
    },

    /** Merge an import into the wizard's own rows.
     *
     *  Everything lands additively and deduped, because a student who imports
     *  twice (or who imports after typing) should get their entries back, not a
     *  doubled list they have to prune. The skills path is the one
     *  `processTranscriptCourses` already uses for catalog-derived skills — a
     *  third source of the same lists, merged on the same terms. */
    async handleLinkedInImport(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      // Let the same file be picked again after a failure — without this the
      // input holds the old value and the second attempt fires no change event.
      event.target.value = '';

      const token = ++this._linkedinToken;
      this.linkedinStatus = 'reading';
      this.linkedinWarnings = [];
      this.linkedinSummary = null;

      const res = await api.importLinkedInExport(file, {
        onPhase: phase => { if (token === this._linkedinToken) this.linkedinStatus = phase; }
      });
      // A superseded import is dropped whole, for the same reason a superseded
      // transcript parse is: landing it on top of newer state would add rows the
      // student never saw offered.
      if (token !== this._linkedinToken) return;

      this.linkedinWarnings = res.warnings || [];
      if (!res.success) {
        this.linkedinStatus = 'failed';
        return;
      }

      const seenExperience = new Set(this.experience.map(e => `${e.role}|${e.employer}`.toLowerCase()));
      const added = (res.experience || []).filter(
        e => !seenExperience.has(`${e.role}|${e.employer}`.toLowerCase())
      );
      this.experience = [...this.experience, ...added.map(e => ({ id: nid(), ...e, ...polishFields() }))];

      const seenProjects = new Set(this.projects.map(p => (p.name || '').toLowerCase()));
      const addedProjects = (res.projects || []).filter(p => !seenProjects.has((p.name || '').toLowerCase()));
      this.projects = [...this.projects, ...addedProjects.map(p => ({ id: nid(), ...p, ...polishFields() }))];

      // Attributes join the academic lists rather than the activities ones so
      // they land on the review screen the student already visits, and so the
      // evidence-id ordering in `collectProfileItems` is unaffected — imported
      // entries are indistinguishable from typed ones by the time it runs.
      const skills = this.mergeInto('skills', res.skills, this.claimedSkills);
      const certifications = this.mergeInto('certifications', res.certifications, new Set(
        [...(this.academic.certifications || []), ...(this.activities.certifications || [])]
          .map(x => x.toLowerCase())
      ));
      const honors = this.mergeInto('honors', res.honors, new Set(
        (this.academic.honors || []).map(x => x.toLowerCase())
      ));

      this.linkedinSummary = {
        experience: added.length, projects: addedProjects.length, skills, certifications, honors,
        filesRead: res.filesRead || []
      };
      this.linkedinStatus = 'done';
    },

    /** Append the values not already claimed to `academic[key]`; returns how
     *  many were actually new. */
    mergeInto(key, values, claimed) {
      const fresh = (values || []).filter(v => !claimed.has(v.toLowerCase()));
      if (fresh.length) this.academic[key] = [...(this.academic[key] || []), ...fresh];
      return fresh.length;
    },

    get experienceCards() {
      return this.experience.map(exp => ({
        id: exp.id,
        roleEmployer: [exp.role, exp.employer].filter(Boolean).join(' — '),
        dates: [exp.start, exp.end].filter(Boolean).join(' – '),
        description: exp.description
      }));
    },
    get projectCards() {
      return this.projects.map(p => ({
        id: p.id,
        nameTech: [p.name, p.tech ? `(${p.tech})` : ''].filter(Boolean).join(' '),
        description: p.description
      }));
    },

    // ---- market analysis -----------------------------------------------

    /** Only the student's own approved data goes into the match — market
     *  demand decides which skills are worth checking, never whether the
     *  student has them.
     *
     *  Shaped by api-service's `toMatchProfile`, not here: the ids it assigns
     *  have to agree with the ones the server mints for the resume request, so
     *  the ordering is defined in one place. See the "Profile shaping" note in
     *  js/services/api-service.js. */
    buildStudentProfile() {
      return api.toMatchProfile(this.academic, this.activities, this.experience, this.projects);
    },

    async runAnalysis() {
      if (this._analysisRunning) return;
      this._analysisRunning = true;

      ANALYSIS_MESSAGES.forEach((message, i) => setTimeout(() => {
        if (this.step === 8) this.analysisMessage = message;
      }, i * MESSAGE_INTERVAL_MS));

      const [profile] = await Promise.all([
        api.analyzeMarket({
          role: this.career.role,
          location: this.career.location,
          experience_level: LEVEL_MAP[this.career.level] || 'entry_level',
          remote_preference: REMOTE_MAP[this.career.remote] || 'any'
        }),
        delayMin(ANALYSIS_MESSAGES.length * MESSAGE_INTERVAL_MS + 400)
      ]);

      this.marketProfile = profile;
      this.marketMatch = api.matchMarket(this.buildStudentProfile(), profile);
      this._analysisRunning = false;

      await this.refreshResumeSummary(0);
      if (this.step === 8) this.step = 9;
    },

    get safeMatch() {
      return this.marketMatch || { summary: { top_market_skills_considered: 0, verified_top_skills: 0 }, matches: [], gaps: [] };
    },
    get marketData() {
      if (!this.marketProfile) return { postingsLabel: '—', dataDate: '—' };
      return {
        postingsLabel: this.marketProfile.postings_analyzed.toLocaleString() + ' unique postings',
        dataDate: formatDataDate(this.marketProfile.data_as_of)
      };
    },
    get marketRows() {
      return this.safeMatch.matches.map(m => ({
        skill: m.market_skill,
        freqLabel: Math.round(m.frequency * 100) + '%',
        isVerified: m.status === 'verified',
        isCoursework: m.status === 'coursework',
        isTransferable: m.status === 'transferable',
        isNotVerified: m.status === 'not_verified'
      }));
    },
    get verifiedSummary() {
      const s = this.safeMatch.summary;
      return `${s.verified_top_skills} of the ${s.top_market_skills_considered} most frequently requested skills were verified in your background.`;
    },
    get hasUnverified() { return this.safeMatch.gaps.length > 0; },
    get unverifiedList() { return this.safeMatch.gaps.join(', '); },

    get topJobs() {
      const verified = new Set(this.safeMatch.matches.filter(m => m.status !== 'not_verified').map(m => m.market_skill.toLowerCase()));
      const postings = this.marketProfile ? (this.marketProfile.sample_postings || []) : [];
      return postings
        .map(job => {
          const matched = job.skills.filter(sk => verified.has(sk.toLowerCase())).length;
          return {
            title: job.title, company: job.company, location: job.location,
            matchLabel: `${matched} of ${job.skills.length} matched`,
            skillChips: job.skills.map(sk => ({
              label: sk,
              style: verified.has(sk.toLowerCase())
                ? 'background:var(--color-accent-100);color:var(--color-accent-700)'
                : 'background:var(--color-neutral-200);color:var(--color-text)'
            })),
            _sortKey: matched
          };
        })
        .sort((a, b) => b._sortKey - a._sortKey);
    },
    get hasTopJobs() { return this.topJobs.length > 0; },

    // ---- resume ---------------------------------------------------------

    /** Draft the summary against the real service. There is no local fallback
     *  by design — see the note on `generateResume` in api-service.js. */
    async refreshResumeSummary(variant) {
      // The server clamps this too; bounding it here keeps the counter shown to
      // the student honest instead of incrementing past a value that does
      // nothing.
      const bounded = Math.max(0, Math.min(variant, RESUME_MAX_VARIANT));
      this.resumeStatus = 'drafting';

      const res = await api.generateResume({
        career: this.career,
        academic: this.academic,
        activities: this.activities,
        experience: this.experience,
        projects: this.projects,
        marketProfile: this.marketProfile,
        variant: bounded
      });

      this.resumeSummaryText = res.summary || '';
      // The drafter picks the handful of courses that answer the target role and
      // orders them; every course the student took is a transcript, not a resume
      // line. Taken as ids rather than text because the ids are exact — the
      // validated claim text is the profile string verbatim, but the catalog
      // titles this UI renders are not, so matching on text would miss.
      const education = (res.resume && res.resume.education) || {};
      this.resumeCourseIds = (education.coursework || []).flatMap(c => c.evidence || []);
      // The sentence that replaces the list of titles. Empty when the drafter
      // declined to write one or the validator deleted it, and the list of
      // course titles renders instead — that list is the student's own record,
      // not template prose standing in for a model, so falling back to it is
      // showing what is true rather than manufacturing a substitute.
      this.resumeCourseworkText = (education.coursework_summary || {}).text || '';
      this.resumeWarnings = res.warnings || [];
      // Claims the evidence validator deleted. Shown, not swallowed: "we left
      // this out and here is why" is a usable answer and it keeps the guardrail
      // visible. The full ResumeDocument on `res.resume` carries structured
      // experience/project/education claims that this UI does not render yet —
      // it builds those sections from wizard state below.
      this.resumeDropped = res.dropped || [];
      this.resumeStatus = res.success ? 'done' : 'failed';
      this.summaryVariant = bounded;
    },
    regenerateWording() { this.refreshResumeSummary(this.summaryVariant + 1); },

    get resumeSummary() {
      if (this.resumeStatus === 'drafting') return 'Drafting your summary…';
      if (this.resumeSummaryText) return this.resumeSummaryText;
      // Distinguish "not yet" from "we tried and couldn't" — the warning above
      // says what went wrong, and this line must not contradict it by claiming
      // the analysis is still running.
      return this.resumeStatus === 'failed'
        ? 'No summary was drafted. The rest of your resume below is everything you entered, unchanged.'
        : 'Your summary will appear here once the market analysis finishes.';
    },
    get resumeFailed() { return this.resumeStatus === 'failed'; },
    get resumeDrafting() { return this.resumeStatus === 'drafting'; },
    get canRegenerate() { return this.resumeStatus === 'done' && this.summaryVariant < RESUME_MAX_VARIANT; },
    get hasResumeWarnings() { return (this.resumeWarnings || []).length > 0; },
    get hasResumeDropped() { return (this.resumeDropped || []).length > 0; },
    /** Drop reasons are enum-ish strings from service/resume_evidence.py. */
    get droppedCards() {
      return (this.resumeDropped || []).map(d => ({
        text: d.text,
        label: `${DROP_REASONS[d.reason] || 'Could not be verified'}${d.detail ? ' — ' + d.detail : ''}`
      }));
    },
    /** Market-verified skills first, then anything else the student claimed. */
    get resumeSkillsLine() {
      const verified = this.safeMatch.matches.filter(m => m.status !== 'not_verified').map(m => m.market_skill);
      const approved = [...new Set([...(this.academic.skills || []), ...(this.activities.skills || [])])];
      const extra = approved.filter(sk => !verified.some(v => v.toLowerCase() === sk.toLowerCase()));
      return [...verified, ...extra].join(' · ') || '—';
    },
    get contactLine() {
      return [this.contact.email, this.contact.phone, this.contact.city, this.contact.linkedin, this.contact.portfolio]
        .filter(Boolean).join('  ·  ');
    },
    get educationLine() {
      const a = this.academic;
      return [
        a.degreeLevel,
        a.major && (a.degreeLevel ? 'in ' + a.major : a.major),
        a.minor && ('· Minor: ' + a.minor)
      ].filter(Boolean).join(' ');
    },
    /** The courses the resume shows, in the order it shows them.
     *
     *  A student's approved list is a transcript — every course they took, in
     *  whatever order the extractor found them. A resume line is a selection:
     *  the few that answer the role, strongest first. The drafter makes that
     *  call (it is the one stage that knows the market demand), so when it
     *  returns a selection this follows it, and falls back to the full list
     *  only when there is no draft to follow — a resume that lists everything
     *  beats one with no education section at all.
     *
     *  Indexed through `collectProfileItems` rather than by re-deriving
     *  `course_<i>` here: that function is the sole definition of the id
     *  scheme, and a second copy of it in this file is exactly the drift the
     *  scheme is written once to prevent.
     *
     *  Titles read as phrases, not as transcript rows: the registrar's
     *  abbreviations are expanded and the code and section number dropped —
     *  "ITSE-1345-002 — Intro ORCL&SQL" is a column width, not a course name.
     *  The catalog's official title fills in only where the entry is a bare
     *  code with no name of its own.
     *  Deduplicated, order preserved: distinct courses can resolve to the same
     *  catalog title, and a resume line reading "Data Structures and
     *  Algorithms, Data Structures and Algorithms" is just wrong output. */
    get courseworkTitles() {
      const entries = (this.academic.coursework || []).map(v => String(v == null ? '' : v).trim()).filter(Boolean);
      const ids = api.collectProfileItems(this.academic, this.activities).coursework.map(c => c.id);
      const byId = new Map(ids.map((id, i) => [id, entries[i]]));

      const selected = this.resumeCourseIds.map(id => byId.get(id)).filter(Boolean);
      const titles = (selected.length ? selected : entries).map(entry => {
        const hit = this.catalogResults.find(c =>
          normCode(entry).startsWith(normCode(c.code)) || normCode(c.code).startsWith(normCode(entry)));
        return catalog.readableCourseTitle(entry, { fallbackTitle: hit && hit.catalog_title });
      });
      return [...new Set(titles.filter(Boolean))];
    },
    get hasCoursework() { return this.courseworkLine.length > 0; },
    /** The drafted sentence when there is one, the titles it was drawn from
     *  otherwise. Both describe the same courses; only one is worth a reader's
     *  attention, and it isn't the list of registrar titles. */
    get courseworkLine() {
      return this.resumeCourseworkText || this.courseworkTitles.join(', ');
    },
    get courseworkIsDrafted() { return Boolean(this.resumeCourseworkText); },
    get hasHonors() { return (this.academic.honors || []).length > 0; },
    get honorsLine() { return (this.academic.honors || []).join(', '); },
    get hasExperience() { return this.experience.length > 0; },
    get hasProjects() { return this.projects.length > 0; },
    get hasActivities() {
      const a = this.activities;
      return [...(a.extracurriculars || []), ...(a.leadership || []), ...(a.volunteering || [])].length > 0;
    },
    get activitiesLine() {
      const a = this.activities;
      return [...(a.leadership || []), ...(a.extracurriculars || []), ...(a.volunteering || [])].join(' · ');
    },

    // ---- export ---------------------------------------------------------

    doPrint() { window.print(); },

    buildPlainText() {
      const a = this.academic;
      return [
        this.contact.name, this.contactLine, '',
        'SUMMARY', this.resumeSummary, '',
        'EDUCATION', a.institution + '  ' + a.gradDate, this.educationLine,
        this.hasCoursework ? 'Coursework: ' + this.courseworkLine : '',
        '', 'SKILLS', this.resumeSkillsLine, '',
        this.hasExperience
          ? 'EXPERIENCE\n' + this.experience.map(e => `${e.role} — ${e.employer} (${e.start}-${e.end})\n${e.description}`).join('\n\n')
          : '',
        this.hasProjects
          ? 'PROJECTS\n' + this.projects.map(p => `${p.name} (${p.tech})\n${p.description}`).join('\n\n')
          : '',
        this.hasActivities ? 'ACTIVITIES & LEADERSHIP\n' + this.activitiesLine : ''
      ].filter(Boolean).join('\n');
    },

    buildDocHtml() {
      const body = this.buildPlainText().split('\n').map(l => `<p>${l || '&nbsp;'}</p>`).join('');
      return `<html><head><meta charset="utf-8"></head><body style="font-family:Georgia,serif">${body}</body></html>`;
    },

    downloadFile(name, content, type) {
      const url = URL.createObjectURL(new Blob([content], { type }));
      const a = document.createElement('a');
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },
    downloadDoc() { this.downloadFile('resume.doc', this.buildDocHtml(), 'application/msword'); },
    downloadTxt() { this.downloadFile('resume.txt', this.buildPlainText(), 'text/plain'); }
  };
}

document.addEventListener('alpine:init', () => {
  window.Alpine.data('pathfinder', pathfinder);
});
