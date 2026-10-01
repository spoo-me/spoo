// Landing Page Edit Modal - Handles opening, populating, and locking save for anonymous users

let currentEditingAlias = null;

// Open the landing edit modal and fetch link data
async function openLandingEditModal(alias) {
    currentEditingAlias = alias;
    const modal = document.getElementById('landing-edit-modal');

    if (!modal) {
        console.error('Landing edit modal not found');
        return;
    }

    // Show modal with loading state
    modal.classList.add('landing-edit-active');
    document.body.style.overflow = 'hidden';

    try {
        // Fetch link data from API
        const response = await fetch(`/api/v1/urls?alias=${encodeURIComponent(alias)}`, {
            credentials: 'include'
        });

        if (!response.ok) {
            throw new Error('Failed to fetch link data');
        }

        const data = await response.json();

        // The API returns paginated results, get the first item
        const linkData = data.data && data.data.length > 0 ? data.data[0] : null;

        if (!linkData) {
            // This is expected for anonymous users who can't query the API
            // We treat this as a "soft error" - just populate what we know (alias)
            populateLandingEditForm({ alias: alias });
            return;
        }

        // Populate form with link data
        populateLandingEditForm(linkData);

    } catch (error) {
        console.error('Error fetching link data:', error);

        // Fallback for actual errors (network, etc)
        populateLandingEditForm({ alias: alias });
    }
}

// Populate the landing edit form with link data
function populateLandingEditForm(linkData) {
    // Basic tab
    document.getElementById('landing-edit-alias').value = linkData.alias || '';
    document.getElementById('landing-edit-long-url').value = linkData.long_url || '';

    // Security tab
    const hasPassword = linkData.password_protected || false;
    const passwordStatus = document.getElementById('landing-password-status');

    if (hasPassword) {
        passwordStatus.textContent = '✓ Password is currently set';
        passwordStatus.style.color = '#10b981';
    } else {
        passwordStatus.textContent = 'No password set';
        passwordStatus.style.color = '#6b7280';
    }

    document.getElementById('landing-edit-password').value = '';
    document.getElementById('landing-remove-password-checkbox').checked = false;
    document.getElementById('landing-edit-private-stats').checked = linkData.private_stats || false;

    // Advanced tab
    document.getElementById('landing-edit-max-clicks').value = linkData.max_clicks || '';

    // Handle expiration date
    if (linkData.expire_after) {
        const expireDate = new Date(linkData.expire_after);
        const localDateTime = new Date(expireDate.getTime() - (expireDate.getTimezoneOffset() * 60000))
            .toISOString()
            .slice(0, 16);
        document.getElementById('landing-edit-expire-after').value = localDateTime;
    } else {
        document.getElementById('landing-edit-expire-after').value = '';
    }

    document.getElementById('landing-edit-block-bots').checked = linkData.block_bots || false;

    // Reset to first tab
    const tabs = document.querySelectorAll('#landing-edit-modal .landing-edit-tab');
    const contents = document.querySelectorAll('#landing-edit-modal .landing-edit-tab-content');
    const tabsContainer = document.querySelector('#landing-edit-modal .landing-edit-tabs');

    if (tabsContainer) {
        tabsContainer.setAttribute('data-active', '0');
    }

    tabs.forEach((tab, i) => {
        tab.classList.toggle('landing-edit-active', i === 0);
    });
    contents.forEach((content, i) => {
        content.classList.toggle('landing-edit-active', i === 0);
    });
}

// Close the landing edit modal
function closeLandingEditModal() {
    const modal = document.getElementById('landing-edit-modal');
    if (modal) {
        modal.classList.remove('landing-edit-active');
        document.body.style.overflow = '';
        currentEditingAlias = null;

        // Reset form
        const form = document.getElementById('landing-edit-form');
        if (form) form.reset();
    }
}

// Prompt user to sign up when trying to save
function promptSignupToSave() {
    // Close edit modal
    closeLandingEditModal();

    // Small delay for smooth transition
    setTimeout(() => {
        // Open auth modal with special message
        if (typeof openAuthModal === 'function') {
            openAuthModal('login');

            // Optionally show a notification
            setTimeout(() => {
                if (typeof showNotification === 'function') {
                    showNotification('Sign up to save your changes and manage unlimited links!', 'info');
                }
            }, 500);
        }
    }, 200);
}

// Check if landing edit modal is open
function isLandingEditModalOpen() {
    const modal = document.getElementById('landing-edit-modal');
    return modal && modal.classList.contains('landing-edit-active');
}

// Initialize tab switching for landing edit modal
document.addEventListener('DOMContentLoaded', function () {
    const modal = document.getElementById('landing-edit-modal');
    if (!modal) return;

    // Tab switching
    const tabs = modal.querySelectorAll('.landing-edit-tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', function () {
            const tabName = this.getAttribute('data-tab');

            // Update tab buttons
            tabs.forEach(t => t.classList.remove('landing-edit-active'));
            this.classList.add('landing-edit-active');

            // Update parent container data-active for sliding animation
            const tabsContainer = modal.querySelector('.landing-edit-tabs');
            if (tabsContainer) {
                // Find index of clicked tab
                const index = Array.from(tabs).indexOf(this);
                tabsContainer.setAttribute('data-active', index);
            }

            // Update tab contents
            const contents = modal.querySelectorAll('.landing-edit-tab-content');
            contents.forEach(content => {
                if (content.getAttribute('data-tab') === tabName) {
                    content.classList.add('landing-edit-active');
                } else {
                    content.classList.remove('landing-edit-active');
                }
            });
        });
    });

    // Close modal when clicking backdrop
    const backdrop = modal.querySelector('.landing-edit-backdrop');
    if (backdrop) {
        backdrop.addEventListener('click', closeLandingEditModal);
    }

    // Handle password checkbox
    const removePasswordCheckbox = document.getElementById('landing-remove-password-checkbox');
    const passwordInput = document.getElementById('landing-edit-password');

    if (removePasswordCheckbox && passwordInput) {
        removePasswordCheckbox.addEventListener('change', function () {
            if (this.checked) {
                passwordInput.disabled = true;
                passwordInput.value = '';
            } else {
                passwordInput.disabled = false;
            }
        });
    }
});

// Export functions to global scope
window.openLandingEditModal = openLandingEditModal;
window.closeLandingEditModal = closeLandingEditModal;
window.promptSignupToSave = promptSignupToSave;
window.isLandingEditModalOpen = isLandingEditModalOpen;
