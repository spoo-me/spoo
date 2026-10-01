// Initialize segmented controls for the options dropdown
document.addEventListener('DOMContentLoaded', function () {
    function initSegments() {
        const segs = document.querySelectorAll('.seg');
        segs.forEach(seg => {
            const targetId = seg.getAttribute('data-target');
            const hidden = document.getElementById(targetId);
            const buttons = Array.from(seg.querySelectorAll('button[data-value]'));
            const indexByValue = new Map(buttons.map((b, i) => [b.getAttribute('data-value'), i]));

            function apply(value) {
                if (hidden) { hidden.value = value; }
                const idx = indexByValue.has(value) ? indexByValue.get(value) : 0;
                seg.setAttribute('data-active', String(idx));

                // Update button states
                buttons.forEach((btn, i) => {
                    btn.classList.toggle('active', i === idx);
                });
            }

            apply(hidden ? hidden.value : '');
            buttons.forEach(btn => btn.addEventListener('click', (e) => {
                e.preventDefault();
                apply(btn.getAttribute('data-value') || '');
            }));
        });
    }

    initSegments();

    // Initialize datetime icon clicks
    initDateTimeIcons();
});

function initDateTimeIcons() {
    const datetimeIcons = document.querySelectorAll('.datetime-icon');

    datetimeIcons.forEach(icon => {
        icon.addEventListener('click', function () {
            const input = this.previousElementSibling;
            if (input && input.type === 'datetime-local') {
                input.focus();
                input.showPicker?.();
            }
        });
    });
}

// Global confetti management
window.confettiAnimationId = null;
window.confettiActive = false;

// Create Link Modal Functions
// System-default fqdn (e.g. "spoo.me"). Prefer the explicit setting passed
// from the server because it's the canonical value the backend writes to
// `urls.domain`. Fall back to deriving from hostUrl if the setting wasn't
// exposed — keeps prod-only paths working until everywhere is upgraded.
function _systemDefaultHost() {
    const cfg = window.dashboardConfig || {};
    if (cfg.systemDefaultDomain) return cfg.systemDefaultDomain;
    const raw = cfg.hostUrl
        || document.querySelector('[data-host]')?.getAttribute('data-host')
        || '';
    try {
        const h = new URL(raw).host;
        if (h) return h;
    } catch (_) { /* fall through to manual strip */ }
    const stripped = raw.replace(/^https?:\/\//, '').replace(/\/+$/, '');
    // Never return '' — empty would mis-scope alias/domain submission and
    // reset the picker to an invalid value. Treat the current page host as
    // a last-resort default; it's always at least syntactically a hostname.
    return stripped || window.location.host;
}

function openCreateLinkModal() {
    const modal = document.getElementById('create-link-modal');

    if (modal) {
        modal.classList.add('active');
        document.body.style.overflow = 'hidden';

        // Reset the domain picker back to the system default whenever the
        // modal opens. Sticking with the previous selection would surprise
        // users who don't see the picker change between sessions. The picker
        // is now the prefix inside the alias input — its trigger label
        // re-renders as `https://<fqdn>/` on the setValue call.
        const picker = document.querySelector('#create-link-modal [data-domain-picker]');
        if (picker && window.DomainPicker) {
            window.DomainPicker.setValue(picker, _systemDefaultHost(), { silent: true });
        }

        // Focus on the first input
        setTimeout(() => {
            const firstInput = modal.querySelector('#create-long-url');
            if (firstInput) firstInput.focus();
        }, 100);
    }
}

function closeCreateLinkModal() {
    const modal = document.getElementById('create-link-modal');
    if (modal) {
        modal.classList.remove('active');
        document.body.style.overflow = '';

        // Reset form
        const form = document.getElementById('create-link-form');
        if (form) form.reset();

        // Clear any error states (also refreshes tab error dots)
        if (window.clearFormErrors) window.clearFormErrors(modal);
        if (window.createAliasCheck) window.createAliasCheck.reset();

        // Reset to first tab (content + buttons + indicator)
        if (window.ModalTabs) window.ModalTabs.reset(modal);
    }
}

function openLinkSuccessModal(shortUrl, linkData) {
    const modal = document.getElementById('link-success-modal');
    const input = document.getElementById('created-link-input');
    const visitLink = document.getElementById('created-link-visit');
    const qrImg = document.getElementById('created-link-qr');

    if (modal && input && visitLink) {
        input.value = shortUrl;
        visitLink.href = shortUrl;

        // Show loading state for QR code
        if (qrImg) {
            qrImg.removeAttribute("src");
            qrImg.style.opacity = "0";
            qrImg.parentElement.style.animation = "qr-loading 1.5s infinite";

            const enc = encodeURIComponent(shortUrl);
            qrImg.onload = function () {
                qrImg.parentElement.style.animation = "none";
                qrImg.style.opacity = "1";
                setupQRCodeInteractions();
            };
            qrImg.onerror = function () {
                qrImg.parentElement.style.animation = "none";
                if (typeof showNotification === "function") {
                    showNotification("Failed to load QR code", "error");
                }
            };
            qrImg.src = `https://qr.spoo.me/api/v1/gradient?content=${enc}&size=280&start=%231d1919&end=%23322c29`;
        }

        modal.classList.add('active');
        document.body.style.overflow = 'hidden';

        // Auto-copy the URL to clipboard
        setTimeout(() => {
            try {
                navigator.clipboard.writeText(shortUrl).then(() => {
                    // Update the copy button to show copied state
                    const copyBtn = document.getElementById('btn-copy-created-link');
                    const icon = copyBtn?.querySelector('i');
                    if (copyBtn) {
                        copyBtn.classList.add('copied');
                        if (icon) {
                            icon.className = 'ti ti-check';
                        }
                        setTimeout(() => {
                            copyBtn.classList.remove('copied');
                            if (icon) {
                                icon.className = 'ti ti-copy';
                            }
                        }, 2000);
                    }
                }).catch(() => {
                    // Fallback for older browsers
                    input.select();
                    input.setSelectionRange(0, 99999);
                    document.execCommand('copy');
                    // Update the copy button to show copied state
                    const copyBtn = document.getElementById('btn-copy-created-link');
                    const icon = copyBtn?.querySelector('i');
                    if (copyBtn) {
                        copyBtn.classList.add('copied');
                        if (icon) {
                            icon.className = 'ti ti-check';
                        }
                        setTimeout(() => {
                            copyBtn.classList.remove('copied');
                            if (icon) {
                                icon.className = 'ti ti-copy';
                            }
                        }, 2000);
                    }
                });
            } catch (err) {
                console.log('Auto-copy failed:', err);
            }
        }, 500);

        // Trigger confetti effect — brand palette, celebratory but short.
        setTimeout(() => {
            if (window.confetti && !window.confettiActive) {
                const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                if (prefersReducedMotion) return;

                window.confettiActive = true;
                const palette = ['#7c3aed', '#a78bfa', '#22c55e', '#f59e0b'];

                // Main burst from center
                confetti({
                    particleCount: 90,
                    spread: 75,
                    startVelocity: 40,
                    scalar: 0.9,
                    origin: { y: 0.6 },
                    colors: palette,
                    zIndex: 10000
                });

                // Brief cannon from each side — 800ms trail
                const end = Date.now() + 800;
                (function frame() {
                    if (!window.confettiActive) return;
                    confetti({
                        particleCount: 2,
                        angle: 60,
                        spread: 50,
                        origin: { x: 0, y: 0.7 },
                        colors: palette,
                        zIndex: 10000
                    });
                    confetti({
                        particleCount: 2,
                        angle: 120,
                        spread: 50,
                        origin: { x: 1, y: 0.7 },
                        colors: palette,
                        zIndex: 10000
                    });
                    if (Date.now() < end && window.confettiActive) {
                        window.confettiAnimationId = requestAnimationFrame(frame);
                    } else {
                        window.confettiAnimationId = null;
                        window.confettiActive = false;
                    }
                }());
            }
        }, 100);
    }
}

function closeLinkSuccessModal() {
    const modal = document.getElementById('link-success-modal');
    if (modal) {
        modal.classList.remove('active');
        document.body.style.overflow = '';

        // Stop confetti animation immediately
        window.confettiActive = false;

        if (window.confettiAnimationId) {
            cancelAnimationFrame(window.confettiAnimationId);
            window.confettiAnimationId = null;
        }

        // Clear all existing confetti
        if (window.confetti && window.confetti.reset) {
            confetti.reset();
        }
    }
}

function copyCreatedLinkToClipboard() {
    try {
        const input = document.getElementById('created-link-input');
        const btn = document.getElementById('btn-copy-created-link');
        const icon = btn?.querySelector('i');

        navigator.clipboard.writeText(input.value).then(() => {
            btn?.classList.add('copied');
            if (icon) {
                icon.className = 'ti ti-check';
            }
            if (typeof showNotification === 'function') {
                showNotification('Link copied to clipboard again!', 'success');
            }
            setTimeout(() => {
                btn?.classList.remove('copied');
                if (icon) {
                    icon.className = 'ti ti-copy';
                }
            }, 1200);
        }).catch(() => {
            input.select();
            input.setSelectionRange(0, 99999);
            document.execCommand('copy');
            btn?.classList.add('copied');
            if (icon) {
                icon.className = 'ti ti-check';
            }
            if (typeof showNotification === 'function') {
                showNotification('Link copied to clipboard again!', 'success');
            }
            setTimeout(() => {
                btn?.classList.remove('copied');
                if (icon) {
                    icon.className = 'ti ti-copy';
                }
            }, 1200);
        });
    } catch (_) {
        console.log('Copy failed');
    }
}

function createAnotherLink() {
    closeLinkSuccessModal();
    openCreateLinkModal();
}

// QR Code interaction setup (similar to result.html)
function setupQRCodeInteractions() {
    const qrContainer = document.getElementById('qr-container');
    const qrOverlay = document.getElementById('qr-overlay');

    if (qrContainer && qrOverlay) {
        qrContainer.style.cursor = "pointer";

        qrContainer.onmouseenter = function () {
            qrOverlay.style.opacity = "1";
            qrOverlay.style.visibility = "visible";
        };

        qrContainer.onmouseleave = function () {
            qrOverlay.style.opacity = "0";
            qrOverlay.style.visibility = "hidden";
        };

        qrContainer.onclick = function () {
            downloadQRCodeFromModal();
        };
    }
}

// QR Code download — uses the already-loaded image via canvas to avoid CORS
function downloadQRCodeFromModal() {
    const qrImg = document.getElementById("created-link-qr");
    if (!qrImg || !qrImg.naturalWidth === 0) return;

    const canvas = document.createElement("canvas");
    canvas.width = qrImg.naturalWidth;
    canvas.height = qrImg.naturalHeight;
    canvas.getContext("2d").drawImage(qrImg, 0, 0);

    const link = document.createElement("a");
    link.href = canvas.toDataURL("image/png");
    link.download = "qrcode.png";
    link.click();

    if (typeof showNotification === 'function') {
        showNotification('QR code downloaded successfully!', 'success');
    }
}

// Form Validation Functions

// Adds https:// when the user omits the protocol. Backend expects http(s)://,
// so this is a silent fix-up — the input value is not visually rewritten.
function normalizeUrl(raw) {
    if (!raw) return raw;
    const trimmed = raw.trim();
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    return 'https://' + trimmed;
}

function validateUrl(url) {
    if (!url) return { valid: false, message: 'URL is required' };

    const normalized = normalizeUrl(url);
    if (!/^https?:\/\/.+/i.test(normalized)) {
        return { valid: false, message: 'Enter a valid URL' };
    }

    if (normalized.toLowerCase().includes('spoo.me')) {
        return { valid: false, message: 'Cannot shorten spoo.me URLs' };
    }

    return { valid: true, message: '' };
}

function validateAlias(alias) {
    if (!alias) return { valid: true, message: '' }; // Optional field

    // Alphanumeric branch first: the emoji class includes 0-9#* (keycap
    // bases), so pure-alnum input must get its length feedback here.
    const aliasPattern = /^[a-zA-Z0-9_-]+$/;
    if (aliasPattern.test(alias)) {
        if (alias.length < 3) {
            return { valid: false, message: 'Must be at least 3 characters' };
        }
        if (alias.length > 16) {
            return { valid: false, message: 'Must be at most 16 characters' };
        }
        return { valid: true, message: '' };
    }

    // Coarse emoji gate — the server enforces the real emoji policy and
    // returns precise reasons via check-alias.
    const emojiPattern = /^[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}0-9#*\u200D\uFE0F\u20E3]+$/u;
    if (emojiPattern.test(alias)) {
        return { valid: true, message: '' };
    }

    return { valid: false, message: 'Use letters, numbers, underscores, hyphens — or emoji only' };
}

function validatePassword(password) {
    if (!password) return { valid: true, message: '' }; // Optional field

    if (password.length < 8) {
        return { valid: false, message: 'Password must be at least 8 characters long' };
    }

    if (!/[a-zA-Z]/.test(password)) {
        return { valid: false, message: 'Password must contain at least one letter' };
    }

    if (!/\d/.test(password)) {
        return { valid: false, message: 'Password must contain at least one number' };
    }

    if (!/[@.]/.test(password)) {
        return { valid: false, message: 'Password must contain @ or .' };
    }

    if (/[@.]{2}/.test(password)) {
        return { valid: false, message: 'Password cannot have consecutive @ or . characters' };
    }

    return { valid: true, message: '' };
}

function validateMaxClicks(maxClicks) {
    if (!maxClicks) return { valid: true, message: '' }; // Optional field

    const clicks = parseInt(maxClicks);
    if (isNaN(clicks) || clicks <= 0) {
        return { valid: false, message: 'Max clicks must be a positive number' };
    }

    return { valid: true, message: '' };
}

function validateExpireAfter(expireAfter) {
    if (!expireAfter) return { valid: true, message: '' }; // Optional field

    const expireDate = new Date(expireAfter);
    if (isNaN(expireDate.getTime())) {
        return { valid: false, message: 'Invalid date' };
    }
    if (expireDate <= new Date()) {
        return { valid: false, message: 'Expiration date must be in the future' };
    }

    return { valid: true, message: '' };
}

function showFieldError(fieldId, message) {
    if (message) {
        window.setFieldError(fieldId, message);
    } else {
        window.clearFieldError(fieldId);
    }
}

function validateForm() {
    let isValid = true;

    // Get form values
    const longUrl = document.getElementById('create-long-url').value.trim();
    const alias = document.getElementById('create-alias').value.trim();
    const password = document.getElementById('create-password').value;
    const maxClicks = document.getElementById('create-max-clicks').value;
    const expireAfter = document.getElementById('create-expire-after').value;

    // Validate each field
    const urlValidation = validateUrl(longUrl);
    const aliasValidation = validateAlias(alias);
    const passwordValidation = validatePassword(password);
    const maxClicksValidation = validateMaxClicks(maxClicks);
    const expireAfterValidation = validateExpireAfter(expireAfter);

    // Show errors
    showFieldError('create-long-url', urlValidation.valid ? '' : urlValidation.message);
    showFieldError('create-alias', aliasValidation.valid ? '' : aliasValidation.message);
    showFieldError('create-password', passwordValidation.valid ? '' : passwordValidation.message);
    showFieldError('create-max-clicks', maxClicksValidation.valid ? '' : maxClicksValidation.message);
    showFieldError('create-expire-after', expireAfterValidation.valid ? '' : expireAfterValidation.message);

    return urlValidation.valid && aliasValidation.valid && passwordValidation.valid &&
        maxClicksValidation.valid && expireAfterValidation.valid;
}

// Form Submission
async function submitCreateForm(event) {
    event.preventDefault();

    if (!validateForm()) {
        const modal = document.getElementById('create-link-modal');
        if (window.ModalTabs) window.ModalTabs.jumpToFirstInvalid(modal);
        return;
    }

    const submitBtn = document.getElementById('btn-create-submit');
    const originalText = submitBtn.innerHTML;

    // Show loading state
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<i class="ti ti-loader-2 spinning"></i><span>Creating...</span>';

    try {
        // Prepare form data
        const domainInput = document.getElementById('create-domain');
        const formData = {
            long_url: normalizeUrl(document.getElementById('create-long-url').value.trim()),
            alias: document.getElementById('create-alias').value.trim() || undefined,
            // domain only flows through when the picker rendered (flag on) AND
            // user selected a custom domain. Sending the system default would
            // be a no-op for the backend, so we omit it to keep the payload
            // small and the path through `routes/api_v1/shorten.py` simple.
            domain: domainInput && domainInput.value &&
                domainInput.value !== _systemDefaultHost() ?
                domainInput.value : undefined,
            password: document.getElementById('create-password').value || undefined,
            max_clicks: document.getElementById('create-max-clicks').value ?
                parseInt(document.getElementById('create-max-clicks').value) : undefined,
            expire_after: document.getElementById('create-expire-after').value || undefined,
            block_bots: document.getElementById('create-block-bots').checked,
            private_stats: document.getElementById('create-private-stats').checked
        };

        // Remove undefined values
        Object.keys(formData).forEach(key => {
            if (formData[key] === undefined) {
                delete formData[key];
            }
        });

        // Submit to API
        const response = await authFetch('/api/v1/shorten', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(formData)
        });

        const result = await response.json();

        if (response.ok) {
            // Success - close create modal and show success modal
            closeCreateLinkModal();
            openLinkSuccessModal(result.short_url, result);

            // Refresh the links list to show the new URL
            if (typeof window.fetchData === 'function') {
                setTimeout(() => {
                    window.fetchData();
                }, 500); // Small delay to ensure any confetti animation doesn't interfere
            }
        } else {
            // Handle API errors
            handleApiError(result);
        }

    } catch (error) {
        console.error('Error creating link:', error);
        if (typeof showNotification === 'function') {
            showNotification('Failed to create link. Please try again.', 'error');
        }
    } finally {
        // Reset button state
        submitBtn.disabled = false;
        submitBtn.innerHTML = originalText;
    }
}

function handleApiError(result) {
    // Handle email verification error specifically
    if (result.code === 'EMAIL_NOT_VERIFIED') {
        if (typeof showVerificationModal === 'function') {
            showVerificationModal('create short URLs');
        } else {
            if (typeof showNotification === 'function') {
                showNotification(result.message || 'Email verification required', 'error');
            }
        }
        return;
    }

    // Handle field-specific errors (single field or multi-field details[])
    const fieldMap = {
        'long_url': 'create-long-url',
        'url': 'create-long-url',
        'alias': 'create-alias',
        'password': 'create-password',
        'max_clicks': 'create-max-clicks',
        'expire_after': 'create-expire-after'
    };
    const createModal = document.getElementById('create-link-modal');
    const applied = window.applyServerErrors(createModal, result, fieldMap);
    if (applied > 0) {
        if (window.ModalTabs) window.ModalTabs.jumpToFirstInvalid(createModal);
        return;
    }

    // Handle general errors
    let errorMessage = result.error || 'An error occurred while creating the link';

    // Customize common error messages
    if (errorMessage.includes('Alias already exists')) {
        showFieldError('create-alias', 'This alias is already taken. Please choose another.');
    } else if (errorMessage.includes('Blocked URL')) {
        showFieldError('create-long-url', 'This URL is not allowed to be shortened.');
    } else if (errorMessage.includes('api key lacks required scope')) {
        errorMessage = 'You do not have permission to create links. Please check your account settings.';
    } else if (errorMessage.includes('ratelimit exceeded')) {
        errorMessage = 'You are creating links too quickly. Please wait a moment and try again.';
    }

    // Show general notification
    if (typeof showNotification === 'function') {
        showNotification(errorMessage, 'error');
    }

    // If any per-field error landed, jump to the first invalid tab.
    if (window.ModalTabs) window.ModalTabs.jumpToFirstInvalid(createModal);
}

// Event Listeners
document.addEventListener('DOMContentLoaded', function () {
    // Create Link button (toolbar + empty-state CTA share the same handler)
    const createBtn = document.getElementById('btn-create-link');
    const emptyCreateBtn = document.getElementById('btn-empty-create');
    const openCreateFlow = function (e) {
        e.preventDefault();
        if (checkVerificationBeforeShorten()) {
            openCreateLinkModal();
        }
    };
    createBtn?.addEventListener('click', openCreateFlow);
    emptyCreateBtn?.addEventListener('click', openCreateFlow);

    // Modal close buttons
    const cancelBtn = document.getElementById('btn-cancel-create');
    if (cancelBtn) {
        cancelBtn.addEventListener('click', closeCreateLinkModal);
    }

    // Form submission
    const createForm = document.getElementById('create-link-form');
    if (createForm) {
        createForm.addEventListener('submit', submitCreateForm);
    }

    // Real-time validation on input
    // Skip 'create-alias' — AliasChecker owns its error state on keystroke.
    const inputs = ['create-long-url', 'create-password', 'create-max-clicks', 'create-expire-after'];
    inputs.forEach(inputId => {
        const input = document.getElementById(inputId);
        if (input) {
            input.addEventListener('blur', function () {
                // Clear previous errors when user starts typing
                showFieldError(inputId, '');
            });
        }
    });

    if (window.AliasChecker) {
        // Scope the live availability check to the picker's current domain
        // so the indicator doesn't say "available" against spoo.me when the
        // user has selected a custom tenant.
        const createDomainInput = document.getElementById('create-domain');
        window.createAliasCheck = window.AliasChecker.attach({
            inputId: 'create-alias',
            diceBtn: document.getElementById('create-alias-dice'),
            indicator: document.getElementById('create-alias-status'),
            getDomain: () => {
                if (!createDomainInput) return null;
                const v = createDomainInput.value;
                return v && v !== _systemDefaultHost() ? v : null;
            },
        });
        // Re-fire the check when the user switches domain mid-flow — the
        // (alias, domain) tuple changed so the cached "available" is stale.
        if (createDomainInput) {
            createDomainInput.addEventListener('change', () => {
                window.createAliasCheck?.recheck();
            });
        }
    }

    // Close modal on backdrop click
    const modal = document.getElementById('create-link-modal');
    if (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal || e.target.classList.contains('modal-backdrop')) {
                closeCreateLinkModal();
            }
        });
    }

    // Close modal on X button
    const closeBtn = modal?.querySelector('.modal-close');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeCreateLinkModal);
    }

    // Tab switching handled by ModalTabs (auto-init via modal-tabs.js).

    // ESC key to close modals
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            const createModal = document.getElementById('create-link-modal');
            const successModal = document.getElementById('link-success-modal');

            if (successModal?.classList.contains('active')) {
                closeLinkSuccessModal();
            } else if (createModal?.classList.contains('active')) {
                closeCreateLinkModal();
            }
        }
    });

    // Close modal on backdrop click for success modal
    const successModal = document.getElementById('link-success-modal');
    if (successModal) {
        successModal.addEventListener('click', function (e) {
            if (e.target === successModal || e.target.classList.contains('modal-backdrop')) {
                closeLinkSuccessModal();
            }
        });
    }

    // Success modal actions: copy (download now handled by QR hover overlay)
    const copyBtn = document.getElementById('btn-copy-created-link');
    copyBtn?.addEventListener('click', copyCreatedLinkToClipboard);
});
(function () {
    // Get host URL from window config or fallback to root element
    const rawHost = window.dashboardConfig?.hostUrl || document.querySelector('[data-host]')?.getAttribute('data-host') || '';
    const host = rawHost.replace(/\/+$/, '');
    const displayHost = host ? (host + '/') : '/';

    const els = {
        search: document.getElementById('f-search'),
        status: document.getElementById('f-status'),
        password: document.getElementById('f-password'),
        maxClicks: document.getElementById('f-maxclicks'),
        createdAfter: document.getElementById('f-created-after'),
        createdBefore: document.getElementById('f-created-before'),
        sortBy: document.getElementById('f-sortby'),
        order: document.getElementById('f-order'),
        pageSize: document.getElementById('f-pagesize'),
        // Domain filter — only rendered when the custom_domains flag is on
        // for this user. Null-guarded everywhere it's read.
        domain: document.getElementById('f-domain'),
        apply: document.getElementById('btn-apply'),
        reset: document.getElementById('btn-reset'),
        optionsBtn: document.getElementById('btn-options'),
        optionsDropdown: document.getElementById('options-dropdown'),
        loading: document.getElementById('list-loading'),
        empty: document.getElementById('list-empty'),
        list: document.getElementById('links-list'),
        pagination: document.getElementById('pagination'),
        tpl: document.getElementById('tpl-link-item'),
    };

    let state = {
        page: 1,
        hasNext: false,
        total: 0,
        pageSize: 20,
        sortBy: 'last_click',
        sortOrder: 'descending',
        filters: {},
    };

    function toEpochSeconds(value) {
        if (!value) return undefined;
        try { return Math.floor(new Date(value).getTime() / 1000); } catch { return undefined; }
    }

    function buildQuery() {
        const filter = {};
        if (els.search.value.trim()) filter.search = els.search.value.trim();
        if (els.status.value) filter.status = els.status.value;
        if (els.password.value) filter.passwordSet = els.password.value;
        if (els.maxClicks.value) filter.maxClicksSet = els.maxClicks.value;
        if (els.createdAfter.value) filter.createdAfter = toEpochSeconds(els.createdAfter.value);
        if (els.createdBefore.value) filter.createdBefore = toEpochSeconds(els.createdBefore.value);

        const params = new URLSearchParams();
        params.set('page', String(state.page));
        params.set('pageSize', String(els.pageSize.value || state.pageSize));
        params.set('sortBy', els.sortBy.value || state.sortBy);
        params.set('sortOrder', els.order.value || state.sortOrder);
        if (Object.keys(filter).length) { params.set('filter', JSON.stringify(filter)); }
        // `domain` is a top-level query param on /api/v1/urls (not part of
        // the `filter` JSON blob) because the backend treats it specially —
        // see routes/api_v1/urls.py.
        if (els.domain && els.domain.value) {
            params.set('domain', els.domain.value);
        }
        return params.toString();
    }

    function setLoading(isLoading) {
        els.loading.style.display = isLoading ? 'flex' : 'none';
    }

    function clearList() {
        els.list.innerHTML = '';
    }

    function formatDate(iso) {
        if (!iso) return '—';
        return window.SmartDatetime ? window.SmartDatetime.formatCreated(iso) :
            (() => { try { return new Date(iso).toLocaleString(); } catch { return '—'; } })();
    }

    function formatTs(ts) {
        if (!ts && ts !== 0) return '—';
        return window.SmartDatetime ? window.SmartDatetime.formatLastClick(ts) :
            (() => { try { return new Date(ts * 1000).toLocaleString(); } catch { return '—'; } })();
    }

    function trimProtocol(url) {
        if (!url) return '';
        return String(url).replace(/^https?:\/\//i, '');
    }

    function createItem(it) {
        const node = els.tpl.content.firstElementChild.cloneNode(true);
        const shortA = node.querySelector('.link-short');
        const long = node.querySelector('.link-long');
        const activeBadge = node.querySelector('.badge-active');
        const inactiveBadge = node.querySelector('.badge-inactive');
        const blockedBadge = node.querySelector('.badge-blocked');
        const expiredBadge = node.querySelector('.badge-expired');
        const pwBadge = node.querySelector('.badge-password');
        const mcBadge = node.querySelector('.badge-max-clicks');
        const privBadge = node.querySelector('.badge-private');
        const blockBotsBadge = node.querySelector('.badge-block-bots');
        const created = node.querySelector('.created-date');
        const last = node.querySelector('.last-click-date');
        const total = node.querySelector('.total-clicks-count');

        // Short URL — prefer the URL doc's `domain` so rows on custom
        // hostnames render their actual host instead of the system default.
        // Open links in a new tab using the full URL so custom-domain rows
        // resolve through their real tenant edge. Route through `new URL`
        // so anything that isn't a real http(s) URL (e.g. a malicious
        // `data-host` value that contains `javascript:`) can't slip into the
        // anchor's href as a script URL.
        const rowHost = it.domain || trimProtocol(displayHost).replace(/\/+$/, '');
        shortA.textContent = `${rowHost}/${it.alias || ''}`;
        try {
            shortA.href = new URL(`https://${rowHost}/${it.alias || ''}`).href;
        } catch (_) {
            shortA.href = '#';
        }

        // Long URL
        long.textContent = it.long_url || '';
        long.title = it.long_url || '';

        // Dates and clicks
        created.textContent = formatDate(it.created_at);
        last.textContent = formatTs(it.last_click);
        total.textContent = (it.total_clicks ?? '0');

        // Status badges - show appropriate badge based on status
        if (it.status === 'ACTIVE') {
            activeBadge.style.display = 'inline-flex';
        } else if (it.status === 'INACTIVE') {
            inactiveBadge.style.display = 'inline-flex';
        } else if (it.status === 'BLOCKED') {
            blockedBadge.style.display = 'inline-flex';
            node.classList.add('row-blocked');
        } else if (it.status === 'EXPIRED') {
            expiredBadge.style.display = 'inline-flex';
        }

        // Password badge
        if (it.password_set) {
            pwBadge.style.display = 'inline-flex';
        }

        // Max clicks badge
        if (typeof it.max_clicks === 'number') {
            mcBadge.style.display = 'inline-flex';
            mcBadge.setAttribute('data-tooltip', `Max clicks: ${it.max_clicks}`);
        }

        // Private stats badge
        if (it.private_stats) {
            privBadge.style.display = 'inline-flex';
        }

        // Block bots badge
        if (it.block_bots) {
            blockBotsBadge.style.display = 'inline-flex';
        }

        // Store full URL data on the row for the modal
        node.setAttribute('data-url-data', JSON.stringify(it));
        node.style.cursor = 'pointer';
        node.classList.add('clickable-row');

        return node;
    }

    async function fetchData() {
        setLoading(true);
        els.empty.style.display = 'none';
        try {
            const qs = buildQuery();
            const doFetch = (typeof window.authFetch === 'function') ? window.authFetch : fetch;
            const res = await doFetch(`/api/v1/urls?${qs}`, { credentials: 'include' });
            if (!res.ok) { throw new Error('Request failed'); }
            const data = await res.json();
            state.page = data.page;
            state.pageSize = data.pageSize;
            state.total = data.total;
            state.hasNext = data.hasNext;
            state.sortBy = data.sortBy;
            state.sortOrder = data.sortOrder;

            clearList();
            if (!data.items || data.items.length === 0) {
                els.empty.style.display = 'block';
                document.getElementById('links-table').style.display = 'none';
                els.pagination.style.display = 'none';
                return;
            }
            document.getElementById('links-table').style.display = 'block';
            const frag = document.createDocumentFragment();
            for (const it of data.items) { frag.appendChild(createItem(it)); }
            els.list.appendChild(frag);
            renderPagination();

            // Initialize tooltips for the newly created items
            initializeTooltips();
        } catch (err) {
            clearList();
            els.empty.style.display = 'block';
            document.getElementById('links-table').style.display = 'none';
        } finally {
            setLoading(false);
        }
    }

    // Initialize Tippy.js tooltips for attribute badges
    function initializeTooltips() {
        // Destroy existing tooltips first
        if (window.attributeTooltips) {
            window.attributeTooltips.forEach(instance => instance.destroy());
        }
        window.attributeTooltips = [];

        // Find all tooltip triggers and initialize Tippy.js
        const tooltipTriggers = document.querySelectorAll('.tooltip-trigger[data-tooltip]');

        tooltipTriggers.forEach(element => {
            // Remove the title attribute to prevent native tooltips
            element.removeAttribute('title');

            const instance = tippy(element, {
                content: element.getAttribute('data-tooltip'),
                placement: 'top',
                theme: 'dark',
                animation: 'fade',
                duration: [200, 150],
                delay: [200, 0],
                arrow: true,
                hideOnClick: false,
                trigger: 'mouseenter focus',
                zIndex: 9999
            });
            window.attributeTooltips.push(instance);
        });
    }

    function renderPagination() {
        const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
        if (totalPages <= 1) { els.pagination.style.display = 'none'; return; }
        els.pagination.style.display = 'flex';
        const start = (state.page - 1) * state.pageSize + 1;
        const end = Math.min(state.total, state.page * state.pageSize);
        els.pagination.innerHTML = '';
        const info = document.createElement('div');
        info.className = 'pagination-info';
        info.textContent = `Showing ${start} to ${end} of ${state.total}`;

        const container = document.createElement('div');
        container.className = 'pagination-controls';
        const prev = document.createElement('button'); prev.className = 'btn'; prev.textContent = 'Prev'; prev.disabled = state.page <= 1;
        const next = document.createElement('button'); next.className = 'btn'; next.textContent = 'Next'; next.disabled = !state.hasNext;
        prev.addEventListener('click', () => { if (state.page > 1) { state.page -= 1; fetchData(); } });
        next.addEventListener('click', () => { if (state.hasNext) { state.page += 1; fetchData(); } });
        container.appendChild(prev);
        container.appendChild(next);

        els.pagination.appendChild(info);
        els.pagination.appendChild(container);
    }

    const FILTERS_STORAGE_KEY = 'spoo:links:filters:v1';

    function saveFilters() {
        try {
            const snapshot = {
                search: els.search.value,
                status: els.status.value,
                password: els.password.value,
                maxClicks: els.maxClicks.value,
                createdAfter: els.createdAfter.value,
                createdBefore: els.createdBefore.value,
                sortBy: els.sortBy.value,
                order: els.order.value,
                pageSize: els.pageSize.value,
                domain: els.domain ? els.domain.value : '',
            };
            localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(snapshot));
        } catch (_) { /* localStorage may be unavailable */ }
    }

    function loadFilters() {
        try {
            const raw = localStorage.getItem(FILTERS_STORAGE_KEY);
            if (!raw) return;
            const saved = JSON.parse(raw);
            for (const [key, value] of Object.entries(saved)) {
                if (els[key] && typeof value === 'string') {
                    // Custom dropdown — DomainPicker keeps the trigger label
                    // and visual selected state in sync with the hidden input.
                    if (key === 'domain' && window.DomainPicker) {
                        const wrapper = els.domain.closest('[data-domain-picker]');
                        if (wrapper) {
                            window.DomainPicker.setValue(wrapper, value, { silent: true });
                            continue;
                        }
                    }
                    els[key].value = value;
                }
            }
        } catch (_) { /* ignore malformed state */ }
    }

    function applyFilters() {
        state.page = 1;
        saveFilters();
        fetchData();
        // Close the options dropdown via the shared primitive
        const optionsWrapper = els.optionsBtn?.closest('.dropdown');
        if (optionsWrapper && window.Dropdown) {
            window.Dropdown.close(optionsWrapper);
        }
    }

    function resetFilters() {
        for (const key of ['search', 'status', 'password', 'maxClicks', 'createdAfter', 'createdBefore']) {
            if (els[key]) els[key].value = '';
        }
        if (els.sortBy) els.sortBy.value = 'last_click';
        if (els.order) els.order.value = 'descending';
        // reset segmented visual state
        document.querySelectorAll('.seg').forEach(seg => {
            const targetId = seg.getAttribute('data-target');
            const hidden = document.getElementById(targetId);
            const defaultValue = (targetId === 'f-order') ? 'descending' : '';
            if (hidden) hidden.value = defaultValue;
            seg.setAttribute('data-active', (targetId === 'f-order') ? '0' : '2');
        });
        if (els.pageSize) els.pageSize.value = '20';
        if (els.domain && window.DomainPicker) {
            const wrapper = els.domain.closest('[data-domain-picker]');
            if (wrapper) window.DomainPicker.setValue(wrapper, '', { silent: true });
        }
        applyFilters();
    }

    // wire events
    els.apply.addEventListener('click', applyFilters);
    els.reset.addEventListener('click', resetFilters);
    els.search.addEventListener('keydown', (e) => { if (e.key === 'Enter') { applyFilters(); } });
    // Domain picker fires a `change` event on its hidden input — refetch
    // immediately (no Apply click needed) so the filter chip feels live.
    if (els.domain) {
        els.domain.addEventListener('change', applyFilters);
    }

    // Options dropdown open/close/outside-click handled by the shared Dropdown primitive.

    // segmented controls behavior
    function initSegments() {
        const segs = document.querySelectorAll('.seg');
        segs.forEach(seg => {
            const targetId = seg.getAttribute('data-target');
            const hidden = document.getElementById(targetId);
            const buttons = Array.from(seg.querySelectorAll('button[data-value]'));
            const indexByValue = new Map(buttons.map((b, i) => [b.getAttribute('data-value'), i]));
            function apply(value) {
                if (hidden) { hidden.value = value; }
                const idx = indexByValue.has(value) ? indexByValue.get(value) : 0;
                seg.setAttribute('data-active', String(idx));
            }
            apply(hidden ? hidden.value : '');
            buttons.forEach(btn => btn.addEventListener('click', () => apply(btn.getAttribute('data-value') || '')));
        });
    }

    // Restore saved filter state before segmented visuals hydrate.
    loadFilters();
    initSegments();

    // Expose fetchData globally for other components to refresh the list
    window.fetchData = fetchData;

    // initial load
    fetchData();
})();


