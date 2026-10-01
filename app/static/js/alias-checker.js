/**
 * Alias availability checker + random generator.
 *
 *   window.AliasChecker.attach({
 *       inputId, diceBtn, indicator, getCurrentAlias?, onValidityChange?,
 *   })
 *   window.AliasChecker.randomAlias()
 *
 * Debounces server hits; resolves client-side length/format first.
 * Uses the shared setFieldError/clearFieldError primitive for error text.
 */
(function () {
    const ALPHABET =
        'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    const LENGTH = 7;
    const DEBOUNCE_MS = 300;

    function randomAlias() {
        const rng = window.crypto || window.msCrypto;
        if (rng && rng.getRandomValues) {
            const buf = new Uint32Array(LENGTH);
            rng.getRandomValues(buf);
            let out = '';
            for (let i = 0; i < LENGTH; i++) out += ALPHABET[buf[i] % ALPHABET.length];
            return out;
        }
        let out = '';
        for (let i = 0; i < LENGTH; i++) {
            out += ALPHABET[Math.floor(Math.random() * ALPHABET.length)];
        }
        return out;
    }

    // Coarse gates only — the server's check-alias endpoint is the source
    // of truth (emoji policy, reserved words, collisions).
    const ALNUM_ALIAS = /^[a-zA-Z0-9_-]+$/;
    const EMOJI_ALIAS = /^[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}0-9#*\u200D\uFE0F\u20E3]+$/u;

    function clientValidate(alias) {
        // Alphanumeric branch first: the emoji class includes 0-9#* (keycap
        // bases), so pure-alnum input must get its length feedback here
        // instead of deferring to the server.
        if (ALNUM_ALIAS.test(alias)) {
            if (alias.length < 3) return 'Must be at least 3 characters';
            if (alias.length > 16) return 'Must be at most 16 characters';
            return null;
        }
        if (EMOJI_ALIAS.test(alias)) return null; // server verdict decides
        return 'Use letters, numbers, underscores, hyphens — or emoji only';
    }

    function reasonToMessage(reason) {
        switch (reason) {
            case 'length':
                return 'Must be 3-16 characters (or 1-15 emoji)';
            case 'format':
                return 'Use letters, numbers, underscores, hyphens — or emoji only';
            case 'reserved':
                return 'This alias is reserved';
            case 'emoji_policy':
                return "That emoji combination isn't supported — use single emojis without flags or joined sequences";
            case 'taken':
                return 'This alias is already taken';
            default:
                return 'Alias is not available';
        }
    }

    function attach(options) {
        const input = document.getElementById(options.inputId);
        if (!input) return;
        const { diceBtn, indicator, getCurrentAlias, getDomain, onValidityChange } = options;

        let debounceTimer = null;
        let inFlight = null;

        function setIndicator(state) {
            indicator.classList.remove('show', 'is-checking', 'is-available', 'is-unavailable');
            if (state === 'idle') {
                indicator.innerHTML = '';
                return;
            }
            indicator.classList.add('show');
            if (state === 'loading') {
                indicator.classList.add('is-checking');
                indicator.innerHTML = '<i class="ti ti-loader-2"></i>';
            } else if (state === 'available') {
                indicator.classList.add('is-available');
                indicator.innerHTML = '<i class="ti ti-check"></i>';
            } else {
                indicator.classList.add('is-unavailable');
                indicator.innerHTML = '<i class="ti ti-x"></i>';
            }
        }

        function emit(valid) {
            if (typeof onValidityChange === 'function') onValidityChange(valid);
        }

        function applyResult(alias, valid, message) {
            if (input.value.trim() !== alias) return;
            if (valid) {
                setIndicator('available');
                window.clearFieldError(options.inputId);
                emit(true);
            } else {
                setIndicator('unavailable');
                window.setFieldError(options.inputId, message);
                emit(false);
            }
        }

        function runServerCheck(alias) {
            if (inFlight) inFlight.abort();
            const controller = new AbortController();
            inFlight = controller;
            // Scope the check to the picker's currently-selected domain so
            // we don't tell the user "available" against the system default
            // when they're about to shorten on a custom domain. Empty/falsy
            // getDomain output → omit the param, server falls back to default.
            let url = `/api/v1/shorten/check-alias?alias=${encodeURIComponent(alias)}`;
            if (typeof getDomain === 'function') {
                const dom = getDomain();
                if (dom) url += `&domain=${encodeURIComponent(dom)}`;
            }
            fetch(url, { signal: controller.signal, credentials: 'same-origin' })
                .then(async (r) => {
                    // Parse the body either way — error responses still carry
                    // structured info we may want to surface.
                    const data = await r.json().catch(() => ({}));
                    if (!r.ok) {
                        // 401/403 happen when auth expired mid-typing or the
                        // domain ownership check rejects (shouldn't normally
                        // happen since the picker only lists owned domains,
                        // but defend anyway). Other errors fall through to
                        // a generic message.
                        const err = new Error(
                            data.error || data.detail || 'alias_check_failed',
                        );
                        err.status = r.status;
                        throw err;
                    }
                    return data;
                })
                .then((data) => {
                    if (data.available) {
                        applyResult(alias, true);
                    } else {
                        applyResult(alias, false, reasonToMessage(data.reason));
                    }
                })
                .catch((err) => {
                    if (err.name === 'AbortError') return;
                    if (input.value.trim() !== alias) return;
                    const message =
                        err.status === 401 || err.status === 403
                            ? 'Sign in to check availability on this domain'
                            : 'Could not verify alias — try again';
                    setIndicator('unavailable');
                    window.setFieldError(options.inputId, message);
                    emit(false);
                });
        }

        input.addEventListener('input', () => {
            if (debounceTimer) clearTimeout(debounceTimer);
            if (inFlight) inFlight.abort();

            const alias = input.value.trim();
            if (!alias) {
                setIndicator('idle');
                window.clearFieldError(options.inputId);
                emit(null);
                return;
            }

            if (typeof getCurrentAlias === 'function' && alias === getCurrentAlias()) {
                setIndicator('idle');
                window.clearFieldError(options.inputId);
                emit(true);
                return;
            }

            const clientError = clientValidate(alias);
            if (clientError) {
                setIndicator('unavailable');
                window.setFieldError(options.inputId, clientError);
                emit(false);
                return;
            }

            setIndicator('loading');
            debounceTimer = setTimeout(() => runServerCheck(alias), DEBOUNCE_MS);
        });

        if (diceBtn) {
            diceBtn.addEventListener('click', (e) => {
                e.preventDefault();
                input.value = randomAlias();
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.focus();
            });
        }

        function reset() {
            if (debounceTimer) clearTimeout(debounceTimer);
            if (inFlight) inFlight.abort();
            setIndicator('idle');
        }

        // Re-fire whatever an `input` event would fire — useful when the
        // domain picker changes and the cached "available" state is now
        // stale against a different (alias, domain) tuple.
        function recheck() {
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }

        return { reset, recheck };
    }

    window.AliasChecker = { attach, randomAlias };
})();
