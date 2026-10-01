// Load OAuth providers on page load and initialize all event handlers
document.addEventListener('DOMContentLoaded', function () {
    // Load OAuth providers and profile pictures
    loadOAuthProviders();
    loadProfilePictures();

    // Initialize password strength monitoring
    const passwordInput = document.getElementById('newPassword');
    if (passwordInput) {
        passwordInput.addEventListener('input', function () {
            updatePasswordStrength(this.value);
        });
    }

    // Initialize modal event handlers
    const modal = document.getElementById('setPasswordModal');
    if (modal) {
        modal.addEventListener('click', function (e) {
            if (e.target === modal || e.target.classList.contains('modal-backdrop')) {
                closeModal('setPasswordModal');
            }
        });
    }

    // ESC key to close modals
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') {
            const passwordModal = document.getElementById('setPasswordModal');
            if (passwordModal?.classList.contains('active')) {
                closeModal('setPasswordModal');
            }
        }
    });

    // Handle set password form with enhanced validation
    const setPasswordForm = document.getElementById('setPasswordForm');
    if (setPasswordForm) {
        setPasswordForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            const password = document.getElementById('newPassword').value;
            const confirmPassword = document.getElementById('confirmPassword').value;
            const submitBtn = document.querySelector('button[form="setPasswordForm"][type="submit"]');
            const originalText = submitBtn ? submitBtn.innerHTML : '';

            // Clear previous errors
            showFieldError('newPassword', '');
            showFieldError('confirmPassword', '');

            // Validate password with comprehensive checks
            const passwordValidation = validateAuthPassword(password);
            if (!passwordValidation.isValid) {
                showPasswordRequirements(passwordValidation.missingRequirements);
                return;
            }

            // Check password confirmation
            if (password !== confirmPassword) {
                showFieldError('confirmPassword', 'Passwords do not match');
                return;
            }

            // Show loading state
            if (submitBtn) {
                submitBtn.disabled = true;
                submitBtn.innerHTML = '<i class="ti ti-loader-2 spinning"></i><span>Setting Password...</span>';
            }

            try {
                const response = await fetch('/auth/set-password', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    credentials: 'include',
                    body: JSON.stringify({ password })
                });

                const data = await response.json();

                if (response.ok) {
                    closeModal('setPasswordModal');
                    showNotification(data.message || 'Password set successfully!', 'success');
                    // Reload page to update UI
                    setTimeout(() => location.reload(), 1500);
                } else {
                    // Handle specific field errors
                    if (data.field && data.field === 'password') {
                        showFieldError('newPassword', data.error);
                    } else {
                        showNotification(data.error || 'Failed to set password', 'error');
                    }
                }
            } catch (error) {
                console.error('Error setting password:', error);
                showNotification('Failed to set password. Please try again.', 'error');
            } finally {
                // Reset button state
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = originalText;
                }
            }
        });
    }
});

async function loadOAuthProviders() {
    try {
        const response = await fetch('/oauth/providers', {
            credentials: 'include'
        });

        if (response.ok) {
            const data = await response.json();
            displayOAuthProviders(data.providers);
        } else {
            console.error('Failed to load OAuth providers');
        }
    } catch (error) {
        console.error('Error loading OAuth providers:', error);
    }
}

function displayOAuthProviders(providers) {
    const container = document.getElementById('oauth-providers-container');
    const addProviderButtons = document.querySelector('.oauth-buttons');
    container.innerHTML = '';

    // Get list of connected provider names
    const connectedProviders = providers.map(p => p.provider.toLowerCase());

    if (providers.length === 0) {
        container.innerHTML = `
        <div class="settings-item">
            <span class="setting-label">No connected accounts</span>
            <span class="setting-value">Connect an OAuth provider to enable easy login</span>
        </div>
    `;
    } else {
        providers.forEach(provider => {
            const providerElement = document.createElement('div');
            providerElement.className = 'oauth-provider';

            const providerIcon = getProviderIcon(provider.provider);
            const linkedDate = new Date(provider.linked_at).toLocaleDateString();

            providerElement.innerHTML = `
            <div class="provider-info">
                <div class="provider-icon">${providerIcon}</div>
                <div class="provider-details">
                    <div class="provider-name">${provider.provider.charAt(0).toUpperCase() + provider.provider.slice(1)}</div>
                    <div class="provider-email">${provider.email} • Linked ${linkedDate}</div>
                </div>
            </div>
            <button class="btn btn-sm btn-danger" onclick="unlinkProvider('${provider.provider}')">
                Disconnect
            </button>
        `;

            container.appendChild(providerElement);
        });
    }

    // Update Add Provider section - hide buttons for already connected providers
    const addProviderSection = document.getElementById('add-provider-section');
    if (addProviderButtons && addProviderSection) {
        addProviderButtons.innerHTML = '';

        // Array of available providers with their details
        const availableProviders = [
            {
                name: 'google',
                displayName: 'Google',
                icon: 'ti ti-brand-google',
                action: 'linkGoogleAccount()'
            },
            {
                name: 'github',
                displayName: 'GitHub',
                icon: 'ti ti-brand-github',
                action: 'linkGitHubAccount()'
            },
            {
                name: 'discord',
                displayName: 'Discord',
                icon: 'ti ti-brand-discord',
                action: 'linkDiscordAccount()'
            }
            // Future providers can be added here
        ];

        const unconnectedProviders = availableProviders.filter(
            provider => !connectedProviders.includes(provider.name)
        );

        if (unconnectedProviders.length > 0) {
            // Show the Add Provider section
            addProviderSection.style.display = 'flex';

            unconnectedProviders.forEach(provider => {
                const button = document.createElement('button');
                button.className = 'btn btn-sm btn-outline';
                button.onclick = () => eval(provider.action);
                button.innerHTML = `<i class="${provider.icon}"></i> Connect ${provider.displayName}`;
                addProviderButtons.appendChild(button);
            });
        } else {
            // Hide the entire Add Provider section when all providers are connected
            addProviderSection.style.display = 'none';
        }
    }
}

function getProviderIcon(provider) {
    switch (provider) {
        case 'google':
            return '<i class="ti ti-brand-google"></i>';
        case 'github':
            return '<i class="ti ti-brand-github"></i>';
        case 'discord':
            return '<i class="ti ti-brand-discord"></i>';
        case 'microsoft':
            return '<i class="ti ti-brand-microsoft"></i>';
        default:
            return '<i class="ti ti-user"></i>';
    }
}

function linkGoogleAccount() {
    window.location.href = '/oauth/google/link';
}

function linkGitHubAccount() {
    window.location.href = '/oauth/github/link';
}

function linkDiscordAccount() {
    window.location.href = '/oauth/discord/link';
}

let selectedPictureId = null;

async function loadProfilePictures() {
    const grid = document.getElementById('picture-grid');
    if (!grid) return;

    // Show loading state
    grid.innerHTML = '<div style="text-align: center; padding: 20px; color: var(--text-secondary);"><i class="ti ti-loader-2" style="font-size: 20px; animation: spin 1s linear infinite;"></i></div>';

    try {
        const response = await fetch('/dashboard/profile-pictures', {
            method: 'GET',
            credentials: 'include',
            headers: {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
        });

        if (response.status === 429) {
            throw new Error('Rate limit exceeded. Please try again later.');
        }

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();

        if (data.pictures && Array.isArray(data.pictures)) {
            renderProfilePictures(data.pictures);
        } else {
            throw new Error('Invalid response format');
        }
    } catch (error) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 20px; color: var(--text-secondary);">
                <i class="ti ti-alert-circle" style="font-size: 20px; color: #ef4444;"></i><br>
                <span style="margin-top: 8px; display: block; font-size: 14px;">${error.message}</span>
            </div>
        `;
    }
}

function renderProfilePictures(pictures) {
    const grid = document.getElementById('picture-grid');
    if (!grid) return;

    grid.innerHTML = '';

    if (!pictures || pictures.length === 0) {
        grid.innerHTML = `
            <div style="text-align: center; padding: 20px; color: var(--text-secondary);">
                <span style="font-size: 14px;">No pictures available</span>
            </div>
        `;
        return;
    }

    // Render OAuth provider pictures
    pictures.forEach(picture => {
        const option = document.createElement('div');
        option.className = `picture-option ${picture.is_current ? 'selected' : ''}`;
        option.dataset.pictureId = picture.id;
        option.onclick = () => selectPicture(picture.id, option);

        option.innerHTML = `
            <img src="${picture.url}" alt="Profile" class="picture-preview" loading="lazy">
            ${picture.is_current ? '<div class="current-indicator"><i class="ti ti-check"></i></div>' : ''}
        `;

        grid.appendChild(option);
    });

    // Add custom upload placeholder (frontend-only)
    const uploadOption = document.createElement('div');
    uploadOption.className = 'picture-option disabled';
    uploadOption.innerHTML = `
        <div class="picture-placeholder"><i class="ti ti-upload"></i></div>
        <div class="coming-soon-badge">Soon</div>
    `;
    grid.appendChild(uploadOption);
}

function selectPicture(pictureId, element) {
    // Remove previous selection
    document.querySelectorAll('.picture-option').forEach(opt => {
        opt.classList.remove('selected');
    });

    // Add selection to clicked element
    element.classList.add('selected');
    selectedPictureId = pictureId;

    // Show save button
    const saveBtn = document.getElementById('save-picture-btn');
    if (saveBtn) {
        saveBtn.style.display = 'inline-flex';
    }
}

async function saveSelectedPicture() {
    if (!selectedPictureId) {
        showNotification('Please select a picture first', 'error');
        return;
    }

    try {
        const response = await fetch('/dashboard/profile-pictures', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            credentials: 'include',
            body: JSON.stringify({ picture_id: selectedPictureId })
        });

        const data = await response.json();

        if (response.ok) {
            showNotification(data.message, 'success');
            loadProfilePictures();
            const saveBtn = document.getElementById('save-picture-btn');
            if (saveBtn) {
                saveBtn.style.display = 'none';
            }
            selectedPictureId = null;

            // Sync sidebar and navbar avatars
            updateSidebarAvatar();
            if (typeof updateAuthNav === 'function') {
                updateAuthNav();
            }
        } else if (response.status === 429) {
            showNotification('Rate limit exceeded. Please wait before trying again.', 'error');
        } else {
            showNotification(data.error || 'Failed to save picture', 'error');
        }
    } catch (error) {
        showNotification('Failed to save picture', 'error');
    }
}

function updateSidebarAvatar() {
    // Get the currently selected picture URL
    const selectedOption = document.querySelector('.picture-option.selected img');
    if (!selectedOption) return;

    const newPictureUrl = selectedOption.src;

    // Update sidebar profile image
    const sidebarImg = document.querySelector('.profile-avatar .profile-image');
    const sidebarInitials = document.querySelector('.profile-avatar .profile-initials');

    if (sidebarImg && newPictureUrl) {
        sidebarImg.src = newPictureUrl;
        sidebarImg.style.display = 'block';
        if (sidebarInitials) {
            sidebarInitials.style.display = 'none';
        }

        // Handle image load error
        sidebarImg.onerror = function () {
            this.style.display = 'none';
            if (sidebarInitials) {
                sidebarInitials.style.display = 'block';
            }
        };
    }
}

async function unlinkProvider(provider) {
    if (!confirm(`Are you sure you want to disconnect your ${provider} account?`)) {
        return;
    }

    try {
        const response = await fetch(`/oauth/providers/${provider}/unlink`, {
            method: 'DELETE',
            credentials: 'include'
        });

        const data = await response.json();

        if (response.ok) {
            // Reload providers list
            loadOAuthProviders();
            showNotification(data.message, 'success');
        } else {
            showNotification(data.error || 'Failed to unlink provider', 'error');
        }
    } catch (error) {
        console.error('Error unlinking provider:', error);
        showNotification('Failed to unlink provider', 'error');
    }
}

function showSetPasswordModal() {
    const modal = document.getElementById('setPasswordModal');
    if (modal) {
        modal.classList.add('active');
        modal.style.display = 'flex';
        document.body.style.overflow = 'hidden';

        // Focus on the first input
        setTimeout(() => {
            const firstInput = modal.querySelector('#newPassword');
            if (firstInput) firstInput.focus();
        }, 100);
    }
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('active');
        document.body.style.overflow = '';

        // Hide modal after transition
        setTimeout(() => {
            modal.style.display = 'none';
        }, 400);

        // Reset form if it exists
        const form = modal.querySelector('form');
        if (form) form.reset();

        // Clear any error states
        const errorFields = modal.querySelectorAll('.error');
        errorFields.forEach(field => field.classList.remove('error'));
        const errorMessages = modal.querySelectorAll('.field-error');
        errorMessages.forEach(msg => msg.remove());
    }
}

// Enhanced password validation functions
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

    // Show strength indicator when there's input
    if (strengthContainer) {
        strengthContainer.style.display = 'block';
    }
    if (requirementsList) {
        requirementsList.style.display = 'block';
    }

    const validation = validateAuthPassword(password);

    if (!validation || typeof validation.strengthScore === 'undefined') {
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

    const requirementsList = missingRequirements.map(req =>
        `<li style="margin: 4px 0;"><span style="color: #ef4444; margin-right: 8px;">✗</span>${req}</li>`
    ).join('');

    showFieldError('newPassword', `
        <div style="text-align: left;">
            <div style="margin-bottom: 8px; font-weight: 500;">Password requirements not met:</div>
            <ul style="margin: 0; padding-left: 0; list-style: none;">
                ${requirementsList}
            </ul>
        </div>
    `);
}

function validatePassword(password) {
    if (!password) return { valid: false, message: 'Password is required' };

    if (password.length < 8) {
        return { valid: false, message: 'Password must be at least 8 characters long' };
    }

    if (!/[a-zA-Z]/.test(password)) {
        return { valid: false, message: 'Password must contain at least one letter' };
    }

    if (!/\d/.test(password)) {
        return { valid: false, message: 'Password must contain at least one number' };
    }

    return { valid: true, message: '' };
}

function showFieldError(fieldId, message) {
    const field = document.getElementById(fieldId);
    if (!field) return;

    // Remove existing error
    const existingError = field.parentNode.querySelector('.field-error');
    if (existingError) existingError.remove();

    if (message) {
        // Add error styling
        field.classList.add('error');
        field.style.borderColor = '#ef4444';

        // Add error message
        const errorEl = document.createElement('div');
        errorEl.className = 'field-error';
        errorEl.style.cssText = 'color: #ef4444; font-size: 12px; margin-top: 4px;';

        // Check if message contains HTML
        if (message.includes('<')) {
            errorEl.innerHTML = message;
        } else {
            errorEl.textContent = message;
        }

        field.parentNode.appendChild(errorEl);
    } else {
        // Remove error styling
        field.classList.remove('error');
        field.style.borderColor = '';
    }
}

// Close modal when clicking outside
window.onclick = function (event) {
    const modals = document.querySelectorAll('.modal');
    modals.forEach(modal => {
        if (event.target === modal) {
            const modalId = modal.id;
            closeModal(modalId);
        }
    });
}
