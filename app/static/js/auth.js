function showAuthError(message) {
    const errorEl = document.getElementById('authError');
    if (errorEl) {
        errorEl.textContent = message;
        errorEl.style.display = 'block';
    }
}

function clearAuthError() {
    const errorEl = document.getElementById('authError');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
}

// Password validation functions
function validateAuthPassword(password) {
    if (!password) {
        return {
            isValid: false,
            missingRequirements: ["Password is required"],
            strengthScore: 0
        };
    }

    const missing = [];
    let strengthScore = 0;

    // Basic requirements - only award points if requirement is met
    const hasMinLength = password.length >= 8;
    const hasMaxLength = password.length <= 128;
    const hasUppercase = /[A-Z]/.test(password);
    const hasLowercase = /[a-z]/.test(password);
    const hasNumber = /[0-9]/.test(password);
    const hasSpecialChar = /[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?~`]/.test(password);
    const hasSafeChars = /^[a-zA-Z0-9!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?~`\s]+$/.test(password);

    // Check requirements and add to missing if not met
    if (!hasMinLength) {
        missing.push("At least 8 characters");
    } else {
        strengthScore += 20; // Only award if requirement is met
    }

    if (!hasMaxLength) {
        missing.push("Maximum 128 characters");
    } else {
        strengthScore += 5; // Small bonus for reasonable length
    }

    if (!hasUppercase) {
        missing.push("At least one uppercase letter");
    } else {
        strengthScore += 15;
    }

    if (!hasLowercase) {
        missing.push("At least one lowercase letter");
    } else {
        strengthScore += 15;
    }

    if (!hasNumber) {
        missing.push("At least one number");
    } else {
        strengthScore += 15;
    }

    if (!hasSpecialChar) {
        missing.push("At least one special character");
    } else {
        strengthScore += 15;
    }

    if (!hasSafeChars) {
        missing.push("Contains invalid characters");
    } else {
        strengthScore += 5; // Small bonus for safe characters
    }

    // Additional strength bonuses (only if basic requirements are met)
    if (hasMinLength && password.length >= 12) {
        strengthScore += 5;
    }
    if (hasMinLength && password.length >= 16) {
        strengthScore += 5;
    }

    // Penalties for weak patterns
    if (/(.)\1{2,}/.test(password)) {  // 3+ repeated characters
        strengthScore -= 15;
    }

    if (/(012|123|234|345|456|567|678|789|890|abc|bcd|cde|def)/i.test(password)) {
        strengthScore -= 20;
    }

    // Common weak passwords - heavy penalty
    const weakPatterns = [
        /password/i,
        /123456/i,
        /qwerty/i,
        /admin/i,
        /login/i,
        /welcome/i
    ];

    for (const pattern of weakPatterns) {
        if (pattern.test(password)) {
            strengthScore -= 30;
            break;
        }
    }

    // Ensure score is between 0-100
    strengthScore = Math.max(0, Math.min(100, strengthScore));

    return {
        isValid: missing.length === 0,
        missingRequirements: missing,
        strengthScore: strengthScore
    };
}

function getStrengthLabel(score) {
    if (score < 20) return "Very Weak";
    if (score < 40) return "Weak";
    if (score < 60) return "Fair";
    if (score < 80) return "Good";
    return "Strong";
}

function getStrengthColor(score) {
    if (score < 20) return "#ef4444";  // red
    if (score < 40) return "#f97316";  // orange
    if (score < 60) return "#eab308";  // yellow
    if (score < 80) return "#22c55e";  // green
    return "#16a34a";  // dark green
}

function updatePasswordStrength(password) {
    const strengthContainer = document.getElementById('passwordStrengthContainer');
    const strengthBar = document.getElementById('passwordStrengthBar');
    const strengthLabel = document.getElementById('passwordStrengthLabel');
    const requirementsList = document.getElementById('passwordRequirements');

    // Always hide if not in register mode
    if (typeof authMode === 'undefined' || authMode !== 'register') {
        if (strengthContainer) {
            strengthContainer.style.display = 'none';
        }
        if (requirementsList) {
            requirementsList.style.display = 'none';
        }
        return { isValid: false, missingRequirements: [], strengthScore: 0 };
    }

    // Hide strength indicator if password is empty
    if (!password || password.length === 0) {
        if (strengthContainer) {
            strengthContainer.style.display = 'none';
        }
        if (requirementsList) {
            requirementsList.style.display = 'none';
        }
        return { isValid: false, missingRequirements: ["Password is required"], strengthScore: 0 };
    }

    // Show strength indicator when there's input in register mode
    if (strengthContainer) {
        strengthContainer.style.display = 'block';
    }
    if (requirementsList) {
        requirementsList.style.display = 'block';
    }

    const validation = validateAuthPassword(password);

    // Ensure validation object has all required properties
    if (!validation || typeof validation !== 'object') {
        console.error('validateAuthPassword returned invalid object:', validation);
        return { isValid: false, missingRequirements: [], strengthScore: 0 };
    }

    const strengthScore = validation.strengthScore || 0;

    if (strengthBar) {
        const color = getStrengthColor(strengthScore);
        strengthBar.style.width = `${strengthScore}%`;
        strengthBar.style.backgroundColor = color;
    }

    if (strengthLabel) {
        const label = getStrengthLabel(strengthScore);
        const color = getStrengthColor(strengthScore);
        strengthLabel.textContent = label;
        strengthLabel.style.color = color;
    }

    if (requirementsList) {
        // Ensure validation object has the required properties
        const missingReqs = (validation && validation.missingRequirements) ? validation.missingRequirements : [];

        // Only show missing requirements
        if (missingReqs.length > 0) {
            requirementsList.innerHTML = missingReqs.map(req => {
                return `<li class="requirement-missing"><span class="requirement-icon">✗</span> ${req}</li>`;
            }).join('');
            requirementsList.style.display = 'block';
        } else {
            requirementsList.style.display = 'none';
        }
    }

    return validation;
}

function showPasswordRequirements(missingRequirements) {
    if (missingRequirements.length === 0) return;

    const errorEl = document.getElementById('authError');
    if (errorEl) {
        const requirementsList = missingRequirements.map(req =>
            `<li style="margin: 4px 0;"><span style="color: #ef4444; margin-right: 8px;">✗</span>${req}</li>`
        ).join('');

        errorEl.innerHTML = `
            <div style="text-align: left;">
                <div style="margin-bottom: 8px; font-weight: 500;">Password requirements not met:</div>
                <ul style="margin: 0; padding-left: 0; list-style: none;">
                    ${requirementsList}
                </ul>
            </div>
        `;
        errorEl.style.display = 'block';
    }
}

async function authFetch(input, init) {
    const opts = init || {};
    if (!opts.credentials) { opts.credentials = 'include'; }
    let res = await fetch(input, opts);
    if (res.status !== 401) { return res; }
    try {
        const refreshRes = await fetch('/auth/refresh', { method: 'POST', credentials: 'include' });
        if (!refreshRes.ok) { return res; }
        res = await fetch(input, opts);
        return res;
    } catch (e) {
        return res;
    }
}

async function submitAuth() {
    const email = document.getElementById('authEmail').value.trim();
    const password = document.getElementById('authPassword').value;
    const user_name_input = document.getElementById('authUserName');
    const user_name = user_name_input ? user_name_input.value.trim() : '';

    const isRegister = (typeof authMode !== 'undefined' && authMode === 'register');

    // Validate password on frontend for registration
    if (isRegister) {
        const validation = validateAuthPassword(password);
        if (!validation.isValid) {
            showPasswordRequirements(validation.missingRequirements);
            return;
        }
    }

    const url = isRegister ? '/auth/register' : '/auth/login';
    const body = isRegister ? { email, password, user_name } : { email, password };

    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include',
            body: JSON.stringify(body)
        });
        const data = await res.json().catch(() => ({}));

        if (!res.ok) {
            if (data.missing_requirements && data.missing_requirements.length > 0) {
                showPasswordRequirements(data.missing_requirements);
            } else {
                showAuthError((data && data.error) || 'Something went wrong');
            }
            return;
        }

        closeAuthModal();
        const nextUrl = new URLSearchParams(window.location.search).get('next');
        // Only allow relative paths to prevent open redirect
        const safeUrl = (nextUrl && nextUrl.startsWith('/') && !nextUrl.startsWith('//')) ? nextUrl : '/dashboard';
        window.location.href = safeUrl;
    } catch (e) {
        showAuthError('Something went wrong');
    }
}

async function logout() {
    try {
        const res = await fetch('/auth/logout', { method: 'POST', credentials: 'include' });
        await updateAuthNav();
        if (res.ok) { window.location.href = '/'; }
    } catch (e) { await updateAuthNav(); }
}

async function updateAuthNav() {
    try {
        const res = await authFetch('/auth/me', { credentials: 'include' });
        const loggedIn = res.ok;
        let user = null;
        if (loggedIn) {
            const data = await res.json().catch(() => ({}));
            user = data && data.user ? data.user : null;
        }
        const show = (id, visible, displayType = 'contents') => { const el = document.getElementById(id); if (el) { el.style.display = visible ? displayType : 'none'; } };
        // Desktop
        show('nav-auth', !loggedIn);
        show('nav-profile', loggedIn);
        // Show locked dashboard for anonymous users, hide for logged in
        show('nav-dashboard-locked', !loggedIn);
        // Hide old direct links when logged in (now in dropdown)
        show('nav-dashboard', false);
        show('nav-keys', false);
        // Mobile
        show('m-nav-auth-btn', !loggedIn, 'block');
        show('m-nav-dashboard', loggedIn);
        show('m-nav-keys', loggedIn);
        show('m-nav-logout', loggedIn);
        show('m-nav-profile', loggedIn, 'block');

        if (user) {
            const username = (user.user_name && String(user.user_name).trim()) || (user.email ? String(user.email).split('@')[0] : 'user');

            // Calculate initials for fallback
            const getInitials = (name) => {
                const words = name.split(' ').filter(word => word.length > 0);
                if (words.length >= 2) {
                    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
                }
                return name.substring(0, 2).toUpperCase();
            };

            const initials = getInitials(username);

            // Handle desktop navbar profile avatar
            const profileContainer = document.querySelector('.navbar .profile-avatar-container');
            if (profileContainer) {
                const img = profileContainer.querySelector('img');
                const initialsDiv = profileContainer.querySelector('#profileInitials');

                if (img && initialsDiv) {
                    if (user.pfp && user.pfp.url) {
                        img.src = user.pfp.url;
                        img.alt = username;
                        img.style.display = 'block';
                        initialsDiv.style.display = 'none';

                        // Handle image load failure
                        img.onerror = function () {
                            img.style.display = 'none';
                            initialsDiv.style.display = 'flex';
                            initialsDiv.textContent = initials;
                        };
                    } else {
                        img.style.display = 'none';
                        initialsDiv.style.display = 'flex';
                        initialsDiv.textContent = initials;
                    }
                }
            }

            // Handle mobile navbar profile avatar
            const mobileProfileContainer = document.querySelector('.mobile-navbar .profile-avatar-container');
            if (mobileProfileContainer) {
                const mimg = mobileProfileContainer.querySelector('img');
                const minitialsDiv = mobileProfileContainer.querySelector('#profileInitials');

                if (mimg && minitialsDiv) {
                    if (user.pfp && user.pfp.url) {
                        mimg.src = user.pfp.url;
                        mimg.alt = username;
                        mimg.style.display = 'block';
                        minitialsDiv.style.display = 'none';

                        // Handle image load failure
                        mimg.onerror = function () {
                            mimg.style.display = 'none';
                            minitialsDiv.style.display = 'flex';
                            minitialsDiv.textContent = initials;
                        };
                    } else {
                        mimg.style.display = 'none';
                        minitialsDiv.style.display = 'flex';
                        minitialsDiv.textContent = initials;
                    }
                }
            }

            // Legacy handling for old profile avatars (in case they still exist)
            const img = document.getElementById('profileAvatar');
            if (img) {
                const avatarUrl = (user.pfp && user.pfp.url)
                    ? user.pfp.url
                    : `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`;
                img.src = avatarUrl;
                img.alt = username;
                img.onerror = function () {
                    if (this.src !== `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`) {
                        this.src = `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`;
                    }
                };
            }

            const mimg = document.getElementById('mProfileAvatar');
            if (mimg) {
                const avatarUrl = (user.pfp && user.pfp.url)
                    ? user.pfp.url
                    : `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`;
                mimg.src = avatarUrl;
                mimg.alt = username;
                mimg.onerror = function () {
                    if (this.src !== `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`) {
                        this.src = `https://avatar.iran.liara.run/username?username=${encodeURIComponent(username)}`;
                    }
                };
            }
        }

        window.authCheckComplete = true;
        window.isLoggedIn = loggedIn;
        document.dispatchEvent(new CustomEvent('auth:init', { detail: { loggedIn, user } }));
    } catch (e) {
        /* default to logged out */
        window.authCheckComplete = true;
        window.isLoggedIn = false;
        document.dispatchEvent(new CustomEvent('auth:init', { detail: { loggedIn: false, user: null } }));
    }
}

document.addEventListener('DOMContentLoaded', function () {
    if (typeof updateAuthNav === 'function') {
        updateAuthNav();
    }

    // Set up password input event listener
    const passwordInput = document.getElementById('authPassword');
    if (passwordInput) {
        passwordInput.addEventListener('input', function () {
            handlePasswordInput(this.value);
        });
    }
});

function handlePasswordInput(password) {
    // Only show strength indicator in register mode
    if (typeof authMode !== 'undefined' && authMode === 'register') {
        updatePasswordStrength(password);
    }
}


