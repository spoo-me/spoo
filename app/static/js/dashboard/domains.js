// Custom Domains — 4-step modal w/ stepper, status panel, confetti.
//
// Modes: add | dns | verify | live | revoked
//   add / revoked → narrow shell, no stepper
//   dns / verify  → wide shell, vertical stepper rail
//   live          → medium shell, no stepper, full management view
//
// State machine driven by the doc.status from the API:
//   PENDING   → mode `dns` initially; user advances to `verify` after "I've added the records"
//   SUSPENDED → mode `dns` with suspended banner (regression from active)
//   ACTIVE    → mode `live`
//   REVOKED   → mode `revoked`
//
// The "I've added the records" button is purely client-side UX — it doesn't
// change server state; it just advances the stepper into the verify panel
// which triggers an immediate verify + starts the auto-poll loop.

// ── Tuning ────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 10000;  // Quiet auto-refresh; user can Check now any time
const FIRST_LIVE_FLAG_KEY = 'domain_first_live_';  // localStorage prefix for confetti dedupe

// ── DOM refs ──────────────────────────────────────────────────────────────

const el = {
    // List
    listLoading: document.getElementById('domains-loading'),
    listEmpty: document.getElementById('domains-empty'),
    list: document.getElementById('domains-list'),
    rowTpl: document.getElementById('tpl-domain-row'),
    dnsTpl: document.getElementById('tpl-dns-record'),
    newBtn: document.getElementById('btn-new-domain'),

    // Modal shell
    modal: document.getElementById('domainModal'),
    modalContent: document.querySelector('#domainModal .modal-content'),
    modalTitle: document.getElementById('domainModalTitle'),
    modalSub: document.getElementById('domainModalSub'),
    modalBadge: document.getElementById('modalStatusBadge'),
    modalClose: document.getElementById('btn-close-domain-modal'),
    modalFooter: document.getElementById('domain-modal-footer'),

    // Footer buttons
    btnCancel: document.getElementById('btn-domain-modal-cancel'),
    btnSubmit: document.getElementById('btn-domain-modal-submit'),
    btnStepBack: document.getElementById('btn-step-back'),
    btnStepNext: document.getElementById('btn-step-next'),
    btnCancelRegistration: document.getElementById('btn-cancel-registration'),
    btnRemove: document.getElementById('btn-domain-remove'),

    // Stepper
    stepper: document.getElementById('stepper'),
    stepperItems: document.querySelectorAll('#stepper .stepper-item'),

    // Add slot
    addInput: document.getElementById('domain-fqdn'),
    addError: document.getElementById('add-domain-error'),

    // DNS slot
    dnsSub: document.getElementById('dns-sub'),
    dnsRecords: document.getElementById('dns-records'),
    setupNotes: document.getElementById('setup-notes'),
    dnsError: document.getElementById('dns-error'),

    // Verify slot
    statusRowDns: document.getElementById('status-row-dns'),
    statusRowSsl: document.getElementById('status-row-ssl'),
    statusDnsMessage: document.getElementById('status-dns-message'),
    statusSslMessage: document.getElementById('status-ssl-message'),
    btnVerifyFooter: document.getElementById('btn-verify-footer'),
    verifyError: document.getElementById('verify-error'),

    // Live slot
    liveHeroFqdn: document.getElementById('live-hero-fqdn'),
    liveOpenIconLink: document.getElementById('live-open-icon-link'),
    liveShortenCta: document.getElementById('live-shorten-cta'),
    btnRevokeFooter: document.getElementById('btn-revoke-footer'),

    // Routing settings (Part A backend, Part B UI)
    routingForm: document.getElementById('routing-form'),
    routingRoot: document.getElementById('routing-root-redirect'),
    routingNotFound: document.getElementById('routing-not-found-redirect'),
    routingRobots: document.getElementById('routing-robots-txt'),
    routingRobotsCounter: document.getElementById('routing-robots-counter'),
    routingSave: document.getElementById('btn-routing-save'),
    routingReset: document.getElementById('btn-routing-reset'),

    // Revoked slot
    revokedAt: document.getElementById('revoked-at'),
    revokedCreated: document.getElementById('revoked-created'),

    // Confetti
    confettiCanvas: document.getElementById('confetti-canvas'),

    // Revoke confirmation sub-modal (redesigned with cards + type-to-confirm)
    revokeModal: document.getElementById('delete-domain-modal'),
    revokeTitle: document.getElementById('deleteDomainModalTitle'),
    revokeFqdn: document.getElementById('delete-domain-fqdn'),
    revokeUrlCount: document.getElementById('delete-domain-url-count'),
    revokeCards: document.querySelectorAll('#delete-domain-modal .cascade-card'),
    revokeInput: document.getElementById('delete-domain-confirm-input'),
    revokeError: document.getElementById('delete-domain-error'),
    revokeCancel: document.getElementById('btn-cancel-domain-delete'),
    revokeConfirm: document.getElementById('btn-confirm-domain-delete'),
    revokeConfirmLabel: document.getElementById('btn-confirm-domain-delete-label'),

    // Remove (hard delete) sub-modal — REVOKED docs only
    removeModal: document.getElementById('remove-domain-modal'),
    removeFqdn: document.getElementById('remove-domain-fqdn'),
    removeInput: document.getElementById('remove-domain-confirm-input'),
    removeError: document.getElementById('remove-domain-error'),
    removeCancel: document.getElementById('btn-cancel-domain-remove'),
    removeConfirm: document.getElementById('btn-confirm-domain-remove'),
};

// ── State ─────────────────────────────────────────────────────────────────

let currentMode = null;
let currentDomain = null;
let domainsCache = [];
let revokeSelectedCascade = false;   // sync with .cascade-card[aria-pressed]
let revokeUrlCount = 0;              // captured at modal open
let pollTimer = null;
let pollDomainId = null;

// Routing settings: snapshot of doc values at modal open, so the save handler
// can diff against current input values and PATCH only the fields the user
// actually changed (and use explicit `null` for fields they cleared).
let routingLoaded = { root_redirect: null, not_found_redirect: null, custom_robots_txt: null };
let routingSaving = false;

// ── Generic helpers ───────────────────────────────────────────────────────

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

function statusClass(status) {
    return `status-${String(status || '').toLowerCase()}`;
}

function timeAgo(iso) {
    if (!iso) return '';
    const ms = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(ms / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins} min${mins === 1 ? '' : 's'} ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs} hr${hrs === 1 ? '' : 's'} ago`;
    const days = Math.floor(hrs / 24);
    return `${days} day${days === 1 ? '' : 's'} ago`;
}

function formatAbsolute(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleString();
    } catch (_) {
        return '—';
    }
}

function setError(node, msg) {
    if (!node) return;
    if (!msg) {
        node.style.display = 'none';
        node.textContent = '';
        return;
    }
    node.textContent = msg;
    node.style.display = 'block';
}

// ── Modal shell control ───────────────────────────────────────────────────

function setMode(mode) {
    document.querySelectorAll('#domainModal .modal-slot').forEach(slot => {
        slot.style.display = slot.dataset.mode === mode ? '' : 'none';
    });
    currentMode = mode;

    // Width + stepper visibility per mode.
    el.modalContent.dataset.mode = mode;
    const wide = mode === 'dns' || mode === 'verify';
    el.modalContent.dataset.size = wide ? 'wide' : (mode === 'live' ? 'medium' : 'narrow');
    el.modalContent.dataset.showStepper = wide ? 'true' : 'false';
}

function showModal() {
    el.modal.style.display = 'flex';
    el.modal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
}

function closeModal({ push = true } = {}) {
    stopPoll();
    el.modal.style.display = 'none';
    el.modal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    currentDomain = null;
    currentMode = null;
    clearConfetti();
    if (push) syncUrl({});
}

function syncUrl(params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) qs.set(k, v);
    const url = qs.toString() ? `?${qs.toString()}` : window.location.pathname;
    history.replaceState(null, '', url);
}

// ── Footer button orchestration ───────────────────────────────────────────

function setFooter({
    cancel = false,
    submit = false,
    next = false,
    back = false,
    remove = false,
    verify = false,
    revoke = false,
    cancelRegistration = false,
}) {
    el.btnCancel.style.display = cancel ? '' : 'none';
    el.btnStepBack.style.display = back ? '' : 'none';
    el.btnRemove.style.display = remove ? '' : 'none';
    el.btnVerifyFooter.style.display = verify ? '' : 'none';
    el.btnRevokeFooter.style.display = revoke ? '' : 'none';
    el.btnCancelRegistration.style.display = cancelRegistration ? '' : 'none';

    if (submit) {
        el.btnSubmit.style.display = '';
        el.btnSubmit.textContent = submit;
    } else {
        el.btnSubmit.style.display = 'none';
    }

    if (next) {
        el.btnStepNext.style.display = '';
        el.btnStepNext.querySelector('.btn-label').textContent = next;
    } else {
        el.btnStepNext.style.display = 'none';
    }

    const anyVisible =
        cancel || submit || next || back || remove || verify || revoke || cancelRegistration;
    el.modalFooter.style.display = anyVisible ? '' : 'none';
}

// ── Stepper rendering ─────────────────────────────────────────────────────

// Updates the stepper item states. `step` is the currently-active step
// (1-4); `completed` is the set of step numbers shown as completed.
function setStepper(activeStep, { completed = new Set(), failed = null } = {}) {
    el.stepperItems.forEach(item => {
        const n = parseInt(item.dataset.step, 10);
        item.classList.remove('is-active', 'is-completed', 'is-pending', 'is-failed');
        if (failed && n === failed) {
            item.classList.add('is-failed');
        } else if (completed.has(n)) {
            item.classList.add('is-completed');
        } else if (n === activeStep) {
            item.classList.add('is-active');
        } else {
            item.classList.add('is-pending');
        }

        const statusNode = item.querySelector('.stepper-status');
        if (failed && n === failed) statusNode.textContent = 'Action needed';
        else if (completed.has(n)) statusNode.textContent = 'Completed';
        else if (n === activeStep) statusNode.textContent = 'In progress';
        else statusNode.textContent = 'Pending';

        // Keyboard focusability mirrors interactivity (completed or active
        // steps are reachable; pending/failed are not).
        const interactive = completed.has(n) || n === activeStep;
        item.tabIndex = interactive ? 0 : -1;
        item.setAttribute('aria-disabled', interactive ? 'false' : 'true');
    });
}

// Step navigation (user clicking a completed/active step).
function attemptJumpToStep(step) {
    if (!currentDomain) return;
    const status = String(currentDomain.status || '').toUpperCase();
    // Only allow back-navigation between dns (step 2) and verify (step 3)
    // while the domain isn't yet active. Live (step 4) ignores stepper clicks.
    if (status === 'ACTIVE' || status === 'REVOKED') return;
    if (step === 2) {
        renderDnsMode(currentDomain);
    } else if (step === 3) {
        renderVerifyMode(currentDomain, { triggerImmediate: false });
    }
}

// ── List fetch + render ───────────────────────────────────────────────────

async function fetchDomains() {
    el.listLoading.style.display = 'flex';
    el.listEmpty.style.display = 'none';
    el.list.style.display = 'none';
    try {
        const res = await authFetch('/api/v1/custom-domains', {
            headers: { 'Accept': 'application/json' },
        });
        if (!res.ok) throw new Error('failed to fetch domains');
        const data = await res.json();
        domainsCache = data.items || [];
        renderList(domainsCache);
    } catch (err) {
        console.error('domains list fetch failed', err);
        showNotification('Failed to load domains', 'error');
    } finally {
        el.listLoading.style.display = 'none';
    }
}

function renderList(domains) {
    el.list.innerHTML = '';
    if (!domains || domains.length === 0) {
        el.listEmpty.style.display = 'flex';
        el.list.style.display = 'none';
        return;
    }
    el.listEmpty.style.display = 'none';
    el.list.style.display = 'flex';

    const frag = document.createDocumentFragment();
    domains.forEach(d => frag.appendChild(createRow(d)));
    el.list.appendChild(frag);
}

function createRow(domain) {
    const row = el.rowTpl.content.firstElementChild.cloneNode(true);
    row.dataset.id = domain.id;
    row.href = `?domain=${encodeURIComponent(domain.id)}`;

    row.querySelector('.domain-fqdn').textContent = domain.fqdn;

    const badge = row.querySelector('.status-badge');
    const status = String(domain.status || '').toUpperCase();
    badge.textContent = status;
    badge.classList.add(statusClass(status));

    row.querySelector('.domain-row-meta').textContent =
        domain.status === 'ACTIVE'
            ? `Verified ${timeAgo(domain.last_verified_at)}`
            : `Added ${timeAgo(domain.created_at)}`;

    row.addEventListener('click', (e) => {
        e.preventDefault();
        openDomainModal(domain.id);
    });
    return row;
}

// ── Modal: open / close / URL sync ────────────────────────────────────────

function setHeader({ title, sub = null }) {
    el.modalTitle.textContent = title;
    if (sub) {
        el.modalSub.textContent = sub;
        el.modalSub.style.display = '';
    } else {
        el.modalSub.style.display = 'none';
    }
}

function openAddModal({ push = true } = {}) {
    currentDomain = null;
    el.addInput.value = '';
    setError(el.addError, '');
    setHeader({
        title: 'Add a domain',
        sub: "Use a domain you control. We'll guide you through DNS and verification next.",
    });
    el.modalBadge.style.display = 'none';

    setStepper(1);
    setMode('add');
    setFooter({ submit: 'Add domain' });

    showModal();
    if (push) syncUrl({ new: '1' });
    setTimeout(() => el.addInput.focus(), 60);
}

async function openDomainModal(id, { push = true } = {}) {
    if (!id) return;
    currentDomain = null;

    setHeader({ title: 'Loading…' });
    el.modalBadge.style.display = 'none';
    setFooter({});

    showModal();
    if (push) syncUrl({ domain: id });

    try {
        const res = await authFetch(`/api/v1/custom-domains/${encodeURIComponent(id)}`, {
            headers: { 'Accept': 'application/json' },
        });
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `Request failed (${res.status})`);
        }
        const doc = await res.json();
        currentDomain = doc;
        renderForStatus(doc);
    } catch (err) {
        showNotification(`Couldn't load domain: ${err.message}`, 'error');
        closeModal();
    }
}

// ── Render by status ──────────────────────────────────────────────────────

function renderForStatus(doc) {
    setHeader({ title: doc.fqdn });
    const status = String(doc.status || '').toUpperCase();
    el.modalBadge.textContent = status;
    el.modalBadge.className = `status-badge modal-status-badge ${statusClass(status)}`;
    el.modalBadge.style.display = '';

    if (status === 'PENDING' || status === 'SUSPENDED') {
        renderDnsMode(doc);
    } else if (status === 'ACTIVE') {
        renderLiveMode(doc);
    } else if (status === 'REVOKED') {
        renderRevokedMode(doc);
    }
}

function renderDnsMode(doc) {
    stopPoll();
    const status = String(doc.status || '').toUpperCase();
    const isSuspended = status === 'SUSPENDED';

    el.dnsSub.textContent = isSuspended
        ? "Verification was lost — re-check your DNS records below, then continue to re-verify."
        : "Publish the record(s) below at your DNS provider, then continue to verify.";

    const records = doc.dns_records || [];
    renderDnsRecords(el.dnsRecords, records, { withCopy: true });

    if (doc.setup_notes && doc.setup_notes.length) {
        el.setupNotes.innerHTML = '';
        doc.setup_notes.forEach(n => {
            const p = document.createElement('p');
            p.textContent = n;
            el.setupNotes.appendChild(p);
        });
        el.setupNotes.style.display = '';
    } else {
        el.setupNotes.style.display = 'none';
    }

    // last_verification_error is verify-step state and must not leak into
    // the DNS step. SUSPENDED is the one exception — the banner already
    // tells the user verification was lost, and surfacing the underlying
    // reason next to the records helps them diagnose what to fix.
    setError(el.dnsError, isSuspended ? (doc.last_verification_error || '') : '');

    setStepper(2, {
        completed: new Set([1]),
        failed: isSuspended ? 2 : null,
    });
    setMode('dns');
    setFooter({
        next: "I've added the records",
        cancelRegistration: !isSuspended,  // SUSPENDED uses Revoke from Live view instead
    });
}

function renderVerifyMode(doc, { triggerImmediate = true } = {}) {
    stopPoll();

    setHeader({ title: doc.fqdn });

    // Reset status rows to pending state
    setStatusRow(el.statusRowDns, 'pending', friendlyStatusDns(doc));
    setStatusRow(el.statusRowSsl, 'pending', friendlyStatusSsl(doc));

    setError(el.verifyError, '');

    setStepper(3, { completed: new Set([1, 2]) });
    setMode('verify');
    setFooter({ back: true, verify: true, cancelRegistration: true });
    setVerifyBtnBusy(false);

    if (triggerImmediate) {
        verifyAndPoll(doc.id, { immediate: true });
    } else {
        // Still show whatever the doc already says, plus start the poll.
        applyVerifyDocState(doc);
        verifyAndPoll(doc.id, { immediate: false });
    }
}

function renderLiveMode(doc) {
    stopPoll();

    el.liveHeroFqdn.textContent = doc.fqdn;
    el.liveOpenIconLink.href = `https://${doc.fqdn}`;
    // Shorten CTA: deep-link to dashboard/links with the domain pre-selected.
    // Even though links.html doesn't yet honour ?domain= as a default, the
    // param is harmless; PR4.5 Part B will wire it.
    el.liveShortenCta.href = `/dashboard/links?domain=${encodeURIComponent(doc.fqdn)}`;

    populateRoutingForm(doc);

    setStepper(4, { completed: new Set([1, 2, 3]) });
    setMode('live');
    setFooter({ revoke: true });

    // Confetti on the *first* time the user lands on Live for this domain.
    fireConfettiIfFirstLive(doc.id);
}

function renderRevokedMode(doc) {
    stopPoll();

    el.revokedAt.textContent = doc.updated_at
        ? `${formatAbsolute(doc.updated_at)} (${timeAgo(doc.updated_at)})`
        : '—';
    el.revokedCreated.textContent = doc.created_at
        ? `${formatAbsolute(doc.created_at)} (${timeAgo(doc.created_at)})`
        : '—';

    setStepper(4);  // No active step; show as "registration history" only.
    setMode('revoked');
    setFooter({ remove: true });
}

function applyVerifyDocState(doc) {
    const status = String(doc.status || '').toUpperCase();
    if (status === 'ACTIVE') {
        setStatusRow(el.statusRowDns, 'success', 'Domain reachable');
        setStatusRow(el.statusRowSsl, 'success', 'Certificate issued');
    } else if (doc.last_verification_error) {
        // Failure shown on whichever row the error implies; default to DNS.
        const raw = doc.last_verification_error;
        const friendly = humaniseVerifyError(raw, doc.fqdn);
        if (/cert|ssl/i.test(raw)) {
            setStatusRow(el.statusRowDns, 'success', 'Domain reachable');
            setStatusRow(el.statusRowSsl, 'fail', friendly);
        } else {
            setStatusRow(el.statusRowDns, 'fail', friendly);
            setStatusRow(el.statusRowSsl, 'pending', 'Waiting for domain check');
        }
    } else {
        setStatusRow(el.statusRowDns, 'pending', friendlyStatusDns(doc));
        setStatusRow(el.statusRowSsl, 'pending', friendlyStatusSsl(doc));
    }
}

// Verifier + CF errors are terse / jargon-heavy. Map known codes to copy
// that tells the user what's wrong and what to do. Unknown codes pass
// through with the fqdn prefix stripped so they at least read cleaner.
function humaniseVerifyError(raw, fqdn) {
    if (!raw) return '';
    let s = String(raw).trim();
    // Strip "<fqdn>: " prefix our verifiers emit.
    if (fqdn) {
        const prefix = `${fqdn}:`;
        if (s.toLowerCase().startsWith(prefix.toLowerCase())) {
            s = s.slice(prefix.length).trim();
        }
    }
    const code = s.toLowerCase();

    if (code === 'nxdomain' || code.includes('nxdomain')) {
        return "We can't find DNS records for this domain yet. Make sure the CNAME you added has propagated at your DNS provider (this can take a few minutes).";
    }
    if (code.includes('timeout') || code.includes('timed out')) {
        return 'DNS lookup timed out. Your DNS provider is slow or unreachable — usually clears up within a minute.';
    }
    if (code.includes('servfail')) {
        return 'Your DNS provider returned a server error. Check that the records are saved correctly.';
    }
    if (code.includes('mismatch') || code.includes('does not match') || code.includes('expected')) {
        return "The DNS record value doesn't match what we expect. Double-check the value at your DNS provider matches exactly.";
    }
    if (code.includes('no answer') || code.includes('noanswer')) {
        return "We didn't get a DNS answer for this record. Confirm it's published and not behind a proxy (Cloudflare DNS-only / grey cloud).";
    }
    if (code.includes('proxied') || code.includes('orange cloud')) {
        return "This domain is proxied through Cloudflare (orange cloud). Set the record to DNS only (grey cloud) so we can verify it.";
    }
    if (code.includes('ssl') || code.includes('certificate')) {
        return "Certificate hasn't been issued yet. We'll keep checking — this usually completes within a minute or two of the DNS records resolving.";
    }
    if (code.includes('hostname not registered') || code.includes('not found')) {
        return 'This hostname is no longer registered on our edge. Try removing and re-registering the domain.';
    }
    // Unknown code — show the cleaned form so we have a starting point for
    // adding a friendly mapping later.
    return s.charAt(0).toUpperCase() + s.slice(1);
}

// ── DNS records rendering ─────────────────────────────────────────────────

function renderDnsRecords(container, records, { withCopy = true } = {}) {
    container.innerHTML = '';
    if (!records.length) return;
    records.forEach(rec => {
        const node = el.dnsTpl.content.firstElementChild.cloneNode(true);
        node.dataset.type = rec.type;
        node.querySelector('.rec-type-text').textContent = rec.type;
        node.querySelector('.rec-name').textContent = rec.name;
        node.querySelector('.rec-value').textContent = rec.value;

        node.querySelectorAll('.rec-copy-btn').forEach(btn => {
            if (!withCopy) {
                btn.style.display = 'none';
                return;
            }
            const field = btn.dataset.field;  // "name" | "value"
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const text = field === 'name' ? rec.name : rec.value;
                copyToClipboard(text, btn, field);
            });
        });

        container.appendChild(node);
    });
}

async function copyToClipboard(text, btn, fieldName) {
    try {
        await navigator.clipboard.writeText(text);
        const icon = btn.querySelector('i');
        const original = icon.className;
        icon.className = 'ti ti-check';
        btn.classList.add('copied');
        // Show what was actually copied so users don't mis-paste Name into a
        // Value field at their DNS provider.
        const label = fieldName === 'name' ? 'name' : 'value';
        showNotification(`Copied DNS ${label} to clipboard`, 'success');
        setTimeout(() => {
            icon.className = original;
            btn.classList.remove('copied');
        }, 1200);
    } catch (_) {
        showNotification('Copy failed — select and copy manually.', 'error');
    }
}

// ── Add (Step 1) submit ───────────────────────────────────────────────────

async function submitAdd() {
    const fqdn = (el.addInput.value || '').trim();
    if (!fqdn) {
        setError(el.addError, 'Enter a domain (e.g. links.yoursite.com)');
        return;
    }
    setError(el.addError, '');
    el.btnSubmit.disabled = true;
    try {
        const res = await authFetch('/api/v1/custom-domains', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ fqdn }),
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
        showNotification(`${body.fqdn} added — add the DNS records next.`, 'success');
        fetchDomains();  // background refresh
        currentDomain = body;
        syncUrl({ domain: body.id });
        renderForStatus(body);
    } catch (err) {
        setError(el.addError, err.message);
    } finally {
        el.btnSubmit.disabled = false;
    }
}

// ── Step navigation: DNS → Verify, Verify → DNS ───────────────────────────

function stepForwardFromDns() {
    if (!currentDomain) return;
    renderVerifyMode(currentDomain, { triggerImmediate: true });
}

function stepBackFromVerify() {
    if (!currentDomain) return;
    stopPoll();
    renderDnsMode(currentDomain);
}

// ── Verify + auto-poll ────────────────────────────────────────────────────
//
// Loop is silent: every POLL_INTERVAL_MS we hit GET in the background; the
// status panel updates if state changes, otherwise no UI noise. User can
// hit "Check now" (footer btn) any time to short-circuit the wait — that
// fires a POST verify (re-triggers CF probe on the backend) and resets the
// schedule. Button spinner is the only visible indicator while a check is
// in flight; idle state is the resting "Check now" label.

async function verifyAndPoll(id, { immediate = true } = {}) {
    pollDomainId = id;
    setError(el.verifyError, '');
    if (immediate) {
        await pollTick({ method: 'POST' });
    } else {
        scheduleNextPoll();
    }
}

function scheduleNextPoll() {
    stopPoll();
    if (!pollDomainId) return;
    pollTimer = setTimeout(() => pollTick({ method: 'GET' }), POLL_INTERVAL_MS);
}

async function pollTick({ method }) {
    if (!pollDomainId) return;
    setVerifyBtnBusy(true);
    try {
        const url = method === 'POST'
            ? `/api/v1/custom-domains/${encodeURIComponent(pollDomainId)}/verify`
            : `/api/v1/custom-domains/${encodeURIComponent(pollDomainId)}`;
        const init = method === 'POST'
            ? {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify({}),
              }
            : { headers: { 'Accept': 'application/json' } };

        let res;
        try {
            res = await authFetch(url, init);
        } catch (netErr) {
            setError(el.verifyError, `Couldn't reach the server: ${netErr.message}`);
            return;
        }
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
            setError(el.verifyError, body.error || `Server returned ${res.status}.`);
            return;
        }

        // Modal may have been closed / changed while we were in flight.
        if (!currentDomain || currentDomain.id !== body.id || currentMode !== 'verify') {
            stopPoll();
            return;
        }

        currentDomain = body;
        applyVerifyDocState(body);

        const status = String(body.status || '').toUpperCase();
        if (status === 'ACTIVE') {
            stopPoll();
            showNotification("Domain verified — it's now live.", 'success');
            renderLiveMode(body);
            fetchDomains();
            return;
        }

        // Domain-side failures (NXDOMAIN, etc.) live on the status row only.
        setError(el.verifyError, '');
    } finally {
        setVerifyBtnBusy(false);
        if (currentMode === 'verify' && currentDomain && currentDomain.id === pollDomainId) {
            scheduleNextPoll();
        }
    }
}

function stopPoll() {
    if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
    }
}

function setVerifyBtnBusy(busy) {
    if (!el.btnVerifyFooter) return;
    el.btnVerifyFooter.disabled = busy;
    const icon = el.btnVerifyFooter.querySelector('.btn-verify-icon');
    const label = el.btnVerifyFooter.querySelector('.btn-label');
    if (icon) icon.classList.toggle('spinning', busy);
    if (label) label.textContent = busy ? 'Checking…' : 'Check now';
}

// ── Status panel helpers ──────────────────────────────────────────────────

function setStatusRow(row, state /* 'pending'|'success'|'fail' */, message) {
    row.dataset.state = state;
    const msgNode = row.querySelector('.status-message');
    if (msgNode) msgNode.textContent = message;
}

// CF state → friendly copy. Centralised so we can extend as we observe new
// CF statuses in the wild.
function friendlyStatusDns(doc) {
    const s = (doc.cf_status || '').toLowerCase();
    if (s === 'active') return 'Domain reachable';
    if (s === 'pending') return 'Waiting for DNS to propagate';
    if (s === 'pending_deployment') return 'Deploying to edge';
    if (s === 'moved') return 'Domain reachable';
    if (s === 'deleted') return 'Domain no longer registered with Cloudflare';
    return 'Checking DNS propagation';
}

function friendlyStatusSsl(doc) {
    const s = (doc.cf_ssl_status || '').toLowerCase();
    if (s === 'active') return 'Certificate issued';
    if (s === 'pending_validation') return 'Waiting for certificate issuance';
    if (s === 'pending_issuance') return 'Issuing certificate';
    if (s === 'pending_deployment') return 'Deploying certificate to edge';
    if (s === 'pending_deletion') return 'Removing certificate';
    if (s === 'deleted') return 'Certificate revoked';
    return 'Waiting for certificate';
}

function humaniseCfStatus(s) {
    if (!s) return '';
    return friendlyStatusDns({ cf_status: s });
}

function humaniseCfSslStatus(s) {
    if (!s) return '';
    return friendlyStatusSsl({ cf_ssl_status: s });
}

// ── Confetti (canvas-based one-shot burst) ────────────────────────────────

let confettiAnimating = false;

function fireConfettiIfFirstLive(domainId) {
    const key = FIRST_LIVE_FLAG_KEY + domainId;
    if (localStorage.getItem(key)) return;
    try { localStorage.setItem(key, '1'); } catch (_) { /* private mode → still fire once */ }
    fireConfetti();
}

function fireConfetti() {
    const canvas = el.confettiCanvas;
    if (!canvas || confettiAnimating) return;

    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = el.modalContent.getBoundingClientRect();
    canvas.style.left = `${rect.left}px`;
    canvas.style.top = `${rect.top}px`;
    canvas.style.width = `${rect.width}px`;
    canvas.style.height = `${rect.height}px`;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    ctx.scale(dpr, dpr);

    const w = rect.width;
    const h = rect.height;
    const colors = ['#a855f7', '#22d3ee', '#34d399', '#fbbf24', '#f472b6', '#60a5fa'];
    const particles = Array.from({ length: 120 }, () => ({
        x: w / 2,
        y: h * 0.35,
        vx: (Math.random() - 0.5) * 8,
        vy: (Math.random() - 1) * 8 - 2,
        size: Math.random() * 6 + 4,
        rot: Math.random() * Math.PI,
        vRot: (Math.random() - 0.5) * 0.3,
        color: colors[Math.floor(Math.random() * colors.length)],
        life: 1,
    }));

    confettiAnimating = true;
    canvas.style.display = 'block';

    let frame = 0;
    const maxFrames = 140;
    function tick() {
        ctx.clearRect(0, 0, w, h);
        particles.forEach(p => {
            p.vy += 0.18;             // gravity
            p.vx *= 0.995;            // air drag
            p.x += p.vx;
            p.y += p.vy;
            p.rot += p.vRot;
            p.life -= 1 / maxFrames;
            ctx.save();
            ctx.globalAlpha = Math.max(0, p.life);
            ctx.translate(p.x, p.y);
            ctx.rotate(p.rot);
            ctx.fillStyle = p.color;
            ctx.fillRect(-p.size / 2, -p.size / 2, p.size, p.size * 0.4);
            ctx.restore();
        });
        frame++;
        if (frame < maxFrames) {
            requestAnimationFrame(tick);
        } else {
            ctx.clearRect(0, 0, w, h);
            canvas.style.display = 'none';
            confettiAnimating = false;
        }
    }
    requestAnimationFrame(tick);
}

function clearConfetti() {
    if (!el.confettiCanvas) return;
    const ctx = el.confettiCanvas.getContext('2d');
    if (ctx) ctx.clearRect(0, 0, el.confettiCanvas.width, el.confettiCanvas.height);
    el.confettiCanvas.style.display = 'none';
    confettiAnimating = false;
}

// ── Cancel-registration (PENDING / VERIFYING docs) ────────────────────────
// Tiny confirm flow: the doc has no URLs yet, so we don't need the cards-as-
// target sub-modal. Native confirm() keeps the cancel path one click away.

async function cancelRegistration() {
    if (!currentDomain) return;
    const fqdn = currentDomain.fqdn;
    const id = currentDomain.id;
    if (!window.confirm(
        `Cancel registration of ${fqdn}?\n\n` +
        `The domain will be removed from your account. You can re-add it later.`
    )) return;

    el.btnCancelRegistration.disabled = true;
    stopPoll();
    try {
        // Revoke then hard-delete so the slot frees immediately.
        const revokeUrl = `/api/v1/custom-domains/${encodeURIComponent(id)}?cascade=false`;
        const revokeRes = await authFetch(revokeUrl, { method: 'DELETE' });
        if (!revokeRes.ok) {
            const body = await revokeRes.json().catch(() => ({}));
            throw new Error(body.error || `Revoke failed (${revokeRes.status})`);
        }
        const removeUrl = `/api/v1/custom-domains/${encodeURIComponent(id)}/permanent`;
        const removeRes = await authFetch(removeUrl, { method: 'DELETE' });
        if (removeRes.status !== 204) {
            const body = await removeRes.json().catch(() => ({}));
            throw new Error(body.error || `Remove failed (${removeRes.status})`);
        }
        showNotification(`${fqdn} registration cancelled`, 'success');
        closeModal();
        fetchDomains();
    } catch (err) {
        showNotification(`Couldn't cancel: ${err.message}`, 'error');
    } finally {
        el.btnCancelRegistration.disabled = false;
    }
}

// ── Revoke sub-modal (cards + type-to-confirm) ────────────────────────────

async function openRevokeModal() {
    if (!currentDomain) return;

    revokeSelectedCascade = false;
    el.revokeCards.forEach(c => {
        const isOrphan = c.dataset.cascade === 'false';
        c.setAttribute('aria-checked', isOrphan ? 'true' : 'false');
        c.tabIndex = isOrphan ? 0 : -1;
        c.classList.toggle('is-selected', isOrphan);
    });

    el.revokeTitle.textContent = `Revoke ${currentDomain.fqdn}?`;
    el.revokeFqdn.textContent = currentDomain.fqdn;
    el.revokeUrlCount.textContent = '…';
    el.revokeInput.value = '';
    el.revokeInput.placeholder = currentDomain.fqdn;
    setError(el.revokeError, '');
    syncRevokeConfirmState();

    el.revokeModal.classList.add('active');
    el.revokeModal.setAttribute('aria-hidden', 'false');

    setTimeout(() => el.revokeInput.focus(), 0);

    try {
        const res = await authFetch(
            `/api/v1/urls?domain=${encodeURIComponent(currentDomain.fqdn)}&pageSize=1`,
            { headers: { 'Accept': 'application/json' } }
        );
        if (res.ok) {
            const body = await res.json();
            revokeUrlCount = body.total || 0;
            el.revokeUrlCount.textContent = String(revokeUrlCount);
        } else {
            revokeUrlCount = 0;
            el.revokeUrlCount.textContent = '?';
        }
    } catch (_) {
        revokeUrlCount = 0;
        el.revokeUrlCount.textContent = '?';
    }
    syncRevokeConfirmState();  // update button label after count loads
}

function closeRevokeModal() {
    el.revokeModal.classList.remove('active');
    el.revokeModal.setAttribute('aria-hidden', 'true');
}

function pickCascadeCard(card) {
    revokeSelectedCascade = card.dataset.cascade === 'true';
    el.revokeCards.forEach(c => {
        const isThis = c === card;
        c.setAttribute('aria-checked', isThis ? 'true' : 'false');
        c.tabIndex = isThis ? 0 : -1;
        c.classList.toggle('is-selected', isThis);
    });
    card.focus();
    syncRevokeConfirmState();
}

function syncRevokeConfirmState() {
    const typed = el.revokeInput.value.trim();
    const fqdnMatches = currentDomain && typed === currentDomain.fqdn;
    el.revokeConfirm.disabled = !fqdnMatches;

    // Dynamic button copy.
    if (revokeSelectedCascade) {
        el.revokeConfirmLabel.textContent =
            revokeUrlCount > 0
                ? `Revoke and delete ${revokeUrlCount} URL${revokeUrlCount === 1 ? '' : 's'}`
                : 'Revoke and delete URLs';
        el.revokeConfirm.classList.add('btn-destructive-strong');
    } else {
        el.revokeConfirmLabel.textContent = 'Revoke domain';
        el.revokeConfirm.classList.remove('btn-destructive-strong');
    }
}

async function confirmRevoke() {
    if (!currentDomain) return;
    if (el.revokeInput.value.trim() !== currentDomain.fqdn) return;

    setError(el.revokeError, '');
    el.revokeConfirm.disabled = true;
    try {
        const cascade = revokeSelectedCascade;
        const url = `/api/v1/custom-domains/${encodeURIComponent(currentDomain.id)}?cascade=${cascade}`;
        const res = await authFetch(url, { method: 'DELETE' });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
        const tail = cascade
            ? ` (${body.urls_deleted} URL(s) deleted)`
            : ' (URLs orphaned)';
        showNotification(`${currentDomain.fqdn} revoked${tail}`, 'success');
        closeRevokeModal();
        closeModal();
        fetchDomains();
    } catch (err) {
        setError(el.revokeError, err.message);
        syncRevokeConfirmState();
    }
}

// ── Remove sub-modal (REVOKED → hard delete) ──────────────────────────────

function openRemoveModal() {
    if (!currentDomain) return;
    el.removeFqdn.textContent = currentDomain.fqdn;
    el.removeInput.value = '';
    el.removeInput.placeholder = currentDomain.fqdn;
    el.removeConfirm.disabled = true;
    setError(el.removeError, '');

    el.removeModal.classList.add('active');
    el.removeModal.setAttribute('aria-hidden', 'false');

    setTimeout(() => el.removeInput.focus(), 0);
}

function closeRemoveModal() {
    el.removeModal.classList.remove('active');
    el.removeModal.setAttribute('aria-hidden', 'true');
}

function syncRemoveConfirmState() {
    if (!currentDomain) {
        el.removeConfirm.disabled = true;
        return;
    }
    el.removeConfirm.disabled = el.removeInput.value.trim() !== currentDomain.fqdn;
}

async function confirmRemove() {
    if (!currentDomain) return;
    if (el.removeInput.value.trim() !== currentDomain.fqdn) return;

    setError(el.removeError, '');
    el.removeConfirm.disabled = true;
    try {
        const url = `/api/v1/custom-domains/${encodeURIComponent(currentDomain.id)}/permanent`;
        const res = await authFetch(url, { method: 'DELETE' });
        if (res.status !== 204) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `Request failed (${res.status})`);
        }
        showNotification(`${currentDomain.fqdn} removed`, 'success');
        closeRemoveModal();
        closeModal();
        fetchDomains();
    } catch (err) {
        setError(el.removeError, err.message);
        syncRemoveConfirmState();
    }
}

// ── Routing settings ──────────────────────────────────────────────────────
//
// Wires the PATCH /api/v1/custom-domains/{id} endpoint to the modal's
// "Routing settings" section. Only rendered when status == ACTIVE; backend
// 422s on any other state.
//
// Save semantics mirror the backend's partial-update contract: only changed
// fields go in the PATCH body, and a field the user cleared is sent as
// explicit `null` (vs. omitted = "leave alone").

function populateRoutingForm(doc) {
    routingLoaded = {
        root_redirect: doc.root_redirect || null,
        not_found_redirect: doc.not_found_redirect || null,
        custom_robots_txt: doc.custom_robots_txt || null,
    };
    el.routingRoot.value = routingLoaded.root_redirect || '';
    el.routingNotFound.value = routingLoaded.not_found_redirect || '';
    el.routingRobots.value = routingLoaded.custom_robots_txt || '';

    updateRobotsCounter();
    syncRoutingDirty();
}

function updateRobotsCounter() {
    if (!el.routingRobotsCounter) return;
    const len = el.routingRobots.value.length;
    el.routingRobotsCounter.textContent = `${len} / 4096`;
}

function _normCurrent() {
    // Empty / whitespace inputs map to null — matches the backend's
    // empty-string normaliser so the diff doesn't show a phantom change
    // when the user only added/removed spaces.
    const norm = (v) => {
        const s = (v || '').trim();
        return s || null;
    };
    return {
        root_redirect: norm(el.routingRoot.value),
        not_found_redirect: norm(el.routingNotFound.value),
        custom_robots_txt: el.routingRobots.value ? el.routingRobots.value : null,
    };
}

function _diffRoutingFields() {
    const current = _normCurrent();
    const body = {};
    for (const key of ['root_redirect', 'not_found_redirect', 'custom_robots_txt']) {
        if (current[key] !== routingLoaded[key]) {
            body[key] = current[key];  // null when cleared, string when set/changed
        }
    }
    return body;
}

function syncRoutingDirty() {
    if (routingSaving) return;
    const body = _diffRoutingFields();
    const dirty = Object.keys(body).length > 0;
    el.routingSave.disabled = !dirty;
    el.routingReset.style.display = dirty ? '' : 'none';
}

function resetRoutingForm() {
    el.routingRoot.value = routingLoaded.root_redirect || '';
    el.routingNotFound.value = routingLoaded.not_found_redirect || '';
    el.routingRobots.value = routingLoaded.custom_robots_txt || '';
    updateRobotsCounter();
    syncRoutingDirty();
}

async function saveRoutingForm() {
    if (!currentDomain || routingSaving) return;
    const body = _diffRoutingFields();
    if (Object.keys(body).length === 0) return;

    routingSaving = true;
    const prevLabel = el.routingSave.textContent;
    el.routingSave.disabled = true;
    el.routingSave.textContent = 'Saving…';

    try {
        const res = await authFetch(
            `/api/v1/custom-domains/${encodeURIComponent(currentDomain.id)}`,
            {
                method: 'PATCH',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(body),
            }
        );
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const msg = _routingErrorMessage(res.status, data);
            showNotification(msg, 'error');
            return;
        }
        // Success — re-seed the loaded snapshot from the server's view so
        // the diff resets to "no pending changes". Don't trust local input
        // text; the backend may have normalised (e.g. trailing-slash on URLs).
        currentDomain = data;
        populateRoutingForm(data);
        showNotification('Routing settings saved.', 'success');
    } catch (err) {
        console.error('routing save failed', err);
        showNotification('Network error — try again.', 'error');
    } finally {
        routingSaving = false;
        el.routingSave.textContent = prevLabel;
        syncRoutingDirty();
    }
}

function _routingErrorMessage(status, body) {
    // Backend error shapes: {detail: "..."} for service-level, or
    // {detail: [{msg: "...", loc: [...]}]} for Pydantic 422s.
    if (status === 422 && Array.isArray(body?.detail) && body.detail[0]?.msg) {
        const first = body.detail[0];
        const fieldLabel = (first.loc || []).slice(-1)[0] || 'field';
        return `${fieldLabel}: ${first.msg}`;
    }
    if (typeof body?.detail === 'string') return body.detail;
    if (status === 403) return "You don't own this domain.";
    if (status === 404) return 'Domain not found.';
    return `Couldn't save settings (HTTP ${status}).`;
}

// ── Wire-up ───────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    fetchDomains();

    const params = new URLSearchParams(window.location.search);
    if (params.get('new')) {
        openAddModal({ push: false });
    } else if (params.get('domain')) {
        openDomainModal(params.get('domain'), { push: false });
    }

    el.newBtn.addEventListener('click', () => openAddModal());

    el.modalClose.addEventListener('click', () => closeModal());
    el.btnCancel.addEventListener('click', () => closeModal());
    el.modal.addEventListener('click', (e) => {
        if (e.target === el.modal || e.target.classList.contains('modal-overlay')) {
            closeModal();
        }
    });

    el.btnSubmit.addEventListener('click', submitAdd);
    el.addInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitAdd();
    });

    el.btnStepNext.addEventListener('click', () => {
        if (currentMode === 'dns') stepForwardFromDns();
    });
    el.btnStepBack.addEventListener('click', () => {
        if (currentMode === 'verify') stepBackFromVerify();
    });

    el.btnVerifyFooter.addEventListener('click', () => {
        if (!pollDomainId || el.btnVerifyFooter.disabled) return;
        stopPoll();
        pollTick({ method: 'POST' });
    });

    el.btnCancelRegistration.addEventListener('click', cancelRegistration);

    // Stepper navigation (mouse + keyboard)
    el.stepperItems.forEach(item => {
        const fire = () => {
            const n = parseInt(item.dataset.step, 10);
            attemptJumpToStep(n);
        };
        item.addEventListener('click', fire);
        item.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                fire();
            }
        });
    });

    // Live mode revoke action (footer)
    el.btnRevokeFooter.addEventListener('click', openRevokeModal);

    // Routing-settings form (only rendered in live mode)
    if (el.routingForm) {
        const onDirty = () => syncRoutingDirty();
        el.routingRoot.addEventListener('input', onDirty);
        el.routingNotFound.addEventListener('input', onDirty);
        el.routingRobots.addEventListener('input', () => {
            updateRobotsCounter();
            syncRoutingDirty();
        });
        el.routingSave.addEventListener('click', saveRoutingForm);
        el.routingReset.addEventListener('click', resetRoutingForm);
        // Submit on Enter from URL inputs (textarea allows Enter for new
        // lines — common robots.txt body has multiple lines).
        el.routingForm.addEventListener('submit', (e) => {
            e.preventDefault();
            saveRoutingForm();
        });
    }

    // Revoke sub-modal
    el.revokeCards.forEach((card, idx) => {
        card.addEventListener('click', () => pickCascadeCard(card));
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                pickCascadeCard(card);
            } else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
                e.preventDefault();
                const next = el.revokeCards[(idx + 1) % el.revokeCards.length];
                pickCascadeCard(next);
            } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
                e.preventDefault();
                const prev = el.revokeCards[
                    (idx - 1 + el.revokeCards.length) % el.revokeCards.length
                ];
                pickCascadeCard(prev);
            }
        });
    });
    el.revokeInput.addEventListener('input', syncRevokeConfirmState);
    el.revokeInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !el.revokeConfirm.disabled) confirmRevoke();
    });
    el.revokeCancel.addEventListener('click', closeRevokeModal);
    el.revokeConfirm.addEventListener('click', confirmRevoke);
    el.revokeModal.addEventListener('click', (e) => {
        if (
            e.target.classList.contains('modal-container') ||
            e.target.classList.contains('modal-backdrop')
        ) {
            closeRevokeModal();
        }
    });

    // Remove sub-modal (REVOKED)
    el.btnRemove.addEventListener('click', openRemoveModal);
    el.removeCancel.addEventListener('click', closeRemoveModal);
    el.removeConfirm.addEventListener('click', confirmRemove);
    el.removeInput.addEventListener('input', syncRemoveConfirmState);
    el.removeInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !el.removeConfirm.disabled) confirmRemove();
    });
    el.removeModal.addEventListener('click', (e) => {
        if (
            e.target.classList.contains('modal-container') ||
            e.target.classList.contains('modal-backdrop')
        ) {
            closeRemoveModal();
        }
    });

    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (el.removeModal.classList.contains('active')) {
            closeRemoveModal();
        } else if (el.revokeModal.classList.contains('active')) {
            closeRevokeModal();
        } else if (el.modal.style.display === 'flex') {
            closeModal();
        }
    });

    window.addEventListener('popstate', () => {
        const p = new URLSearchParams(window.location.search);
        if (p.get('new')) {
            openAddModal({ push: false });
        } else if (p.get('domain')) {
            openDomainModal(p.get('domain'), { push: false });
        } else {
            stopPoll();
            el.modal.style.display = 'none';
            el.modal.setAttribute('aria-hidden', 'true');
            document.body.style.overflow = '';
        }
    });
});
