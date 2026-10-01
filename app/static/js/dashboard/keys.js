// API Keys Management JavaScript
const keyElements = {
    loading: document.getElementById('keys-loading'),
    empty: document.getElementById('keys-empty'),
    table: document.getElementById('keys-table'),
    list: document.getElementById('keys-list'),
    template: document.getElementById('tpl-key-item'),
    newKeyBtn: document.getElementById('btn-new-key'),
    createModal: document.getElementById('createKeyModal'),
    successModal: document.getElementById('keySuccessModal'),
    createBtn: document.getElementById('btn-create'),
    tokenInput: document.getElementById('fullTokenInput'),
    deleteModal: document.getElementById('delete-key-modal'),
    cancelDeleteBtn: document.getElementById('btn-cancel-key-delete'),
    confirmDeleteBtn: document.getElementById('btn-confirm-key-delete')
};

let pendingDeleteKeyId = null;

async function fetchKeys() {
    setKeysLoading(true);
    keyElements.empty.style.display = 'none';

    try {
        const res = await authFetch('/api/v1/keys', { headers: { 'Accept': 'application/json' } });
        if (!res.ok) throw new Error('Failed to fetch keys');

        const data = await res.json();
        renderKeys(data.keys || []);
    } catch (error) {
        console.error('Error fetching keys:', error);
        showEmptyState();
    } finally {
        setKeysLoading(false);
    }
}

function setKeysLoading(loading) {
    keyElements.loading.style.display = loading ? 'flex' : 'none';
    // Don't automatically show table when loading is false - let renderKeys handle it
    if (loading) {
        keyElements.table.style.display = 'none';
        keyElements.empty.style.display = 'none';
    }
}

function showEmptyState() {
    keyElements.empty.style.display = 'flex';
    keyElements.table.style.display = 'none';
}

function renderKeys(keys) {
    keyElements.list.innerHTML = '';

    if (!keys || keys.length === 0) {
        showEmptyState();
        return;
    }

    keyElements.table.style.display = 'block';
    keyElements.empty.style.display = 'none';

    const fragment = document.createDocumentFragment();
    keys.forEach(key => {
        const row = createKeyRow(key);
        fragment.appendChild(row);
    });

    keyElements.list.appendChild(fragment);
}

function createKeyRow(key) {
    const node = keyElements.template.content.firstElementChild.cloneNode(true);

    // Name and description
    const nameEl = node.querySelector('.key-name');
    const descEl = node.querySelector('.key-description');
    nameEl.textContent = key.name || '(no name)';
    descEl.textContent = key.description || '';
    if (!key.description) descEl.style.display = 'none';

    // Key prefix
    const prefixEl = node.querySelector('.key-prefix');
    prefixEl.textContent = `spoo_${key.token_prefix || ''}…`;

    // Scopes
    const scopesEl = node.querySelector('.scopes-list');
    if (key.scopes && key.scopes.length > 0) {
        key.scopes.forEach(scope => {
            const tag = document.createElement('span');
            tag.className = 'scope-tag';
            tag.textContent = scope;
            scopesEl.appendChild(tag);
        });
    } else {
        scopesEl.innerHTML = '<span style="color: var(--text-secondary); font-style: italic;">none</span>';
    }

    // Dates
    const createdEl = node.querySelector('.created-date');
    const expiresEl = node.querySelector('.expires-date');
    createdEl.textContent = key.created_at ? new Date(key.created_at * 1000).toLocaleDateString() : '—';
    expiresEl.textContent = key.expires_at ? new Date(key.expires_at * 1000).toLocaleDateString() : 'Never';

    // Status
    const statusEl = node.querySelector('.status-badge');
    const status = key.revoked ? 'revoked' : (key.expires_at && key.expires_at * 1000 < Date.now() ? 'expired' : 'active');
    statusEl.textContent = status.toUpperCase();
    statusEl.className = `status-badge status-${status}`;

    // Delete/Revoke button
    const revokeBtn = node.querySelector('.btn-revoke');
    revokeBtn.disabled = key.revoked;
    revokeBtn.textContent = key.revoked ? 'Deleted' : 'Delete';
    revokeBtn.setAttribute('data-id', key.id);

    if (!key.revoked) {
        revokeBtn.addEventListener('click', () => openDeleteModal(key));
    }

    return node;
}

function openDeleteModal(key) {
    pendingDeleteKeyId = key.id;
    keyElements.deleteModal.classList.add('active');
    document.body.style.overflow = 'hidden';
    setTimeout(() => keyElements.cancelDeleteBtn?.focus(), 100);
}

function closeDeleteModal() {
    keyElements.deleteModal.classList.remove('active');
    document.body.style.overflow = '';
    pendingDeleteKeyId = null;
}

async function confirmDeleteKey() {
    if (!pendingDeleteKeyId) return;

    const btn = keyElements.confirmDeleteBtn;
    const originalHtml = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="ti ti-loader-2"></i><span>Deleting...</span>';

    try {
        const res = await authFetch(`/api/v1/keys/${pendingDeleteKeyId}`, {
            method: 'DELETE',
            headers: { 'Accept': 'application/json' }
        });

        if (res.ok) {
            const data = await res.json().catch(() => ({}));
            const action = data.action || 'deleted';
            showNotification(`Key ${action} successfully`, 'success');
            closeDeleteModal();
            fetchKeys();
        } else {
            showNotification('Failed to revoke key', 'error');
        }
    } catch (error) {
        showNotification('Failed to revoke key', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
    }
}

// Modal Management
function openCreateKeyModal() {
    keyElements.createModal.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Focus first input
    setTimeout(() => {
        document.getElementById('key-name').focus();
    }, 100);
}

function closeCreateKeyModal() {
    keyElements.createModal.style.display = 'none';
    document.body.style.overflow = '';

    // Reset form
    resetCreateForm();
}

function openKeySuccessModal(token, tokenPrefix) {
    closeCreateKeyModal();

    keyElements.tokenInput.value = token;
    keyElements.successModal.style.display = 'flex';
    document.body.style.overflow = 'hidden';

    // Reload keys list to show the new key
    fetchKeys();

    // Auto-copy token to clipboard
    setTimeout(async () => {
        try {
            await navigator.clipboard.writeText(token);

            // Visual feedback on copy button
            const copyBtn = document.querySelector('.copy-btn i');
            if (copyBtn) {
                copyBtn.className = 'ti ti-check';
                setTimeout(() => {
                    copyBtn.className = 'ti ti-copy';
                }, 2000);
            }
        } catch (error) {
            console.log('Auto-copy failed, user can copy manually');
        }
    }, 100);
}

function closeKeySuccessModal() {
    keyElements.successModal.style.display = 'none';
    document.body.style.overflow = '';
    fetchKeys(); // Refresh the list
}

function resetCreateForm() {
    document.getElementById('key-name').value = '';
    document.getElementById('key-description').value = '';
    document.getElementById('key-expires').value = '';

    // Reset access level to full access (default)
    document.getElementById('full-access').checked = true;
    document.getElementById('custom-access').checked = false;

    // Reset permission checkboxes (will be set by handleAccessLevelChange)
    document.querySelectorAll('.scope-input').forEach(input => {
        input.checked = input.value === 'shorten:create';
    });

    // Reset access level state to full access
    handleAccessLevelChange('full');
}

async function createKey() {
    const name = document.getElementById('key-name').value.trim();
    const description = document.getElementById('key-description').value.trim();
    const expiresAt = document.getElementById('key-expires').value;
    const accessLevel = document.querySelector('input[name="access-level"]:checked').value;

    let scopes;
    if (accessLevel === 'full') {
        scopes = ['admin:all'];
    } else {
        // Custom access uses selected permissions
        scopes = Array.from(document.querySelectorAll('.scope-input:checked')).map(i => i.value);
    }

    if (!name || scopes.length === 0) {
        showNotification('Name and at least one permission are required', 'error');
        return;
    }

    const body = {
        name,
        description: description || undefined,
        scopes,
        expires_at: expiresAt || undefined
    };

    // Disable button and show loading state
    const createBtn = keyElements.createBtn;
    const originalText = createBtn.innerHTML;
    createBtn.disabled = true;
    createBtn.innerHTML = '<i class="ti ti-loader" style="animation: spin 1s linear infinite;"></i> Creating...';

    try {
        const res = await authFetch('/api/v1/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(body)
        });

        const data = await res.json().catch(() => ({}));

        if (!res.ok) {
            // Close modal first so notification is visible
            closeCreateKeyModal();

            // Check for email verification error
            if (data.code === 'EMAIL_NOT_VERIFIED') {
                if (typeof showVerificationModal === 'function') {
                    showVerificationModal('create API keys');
                }
                return;
            }

            // Improved error messages
            let errorMessage = data.error || 'Failed to create key';

            // Handle specific error cases
            if (res.status === 429 || errorMessage.toLowerCase().includes('ratelimit')) {
                errorMessage = 'Rate limit exceeded. You can create up to 5 keys per hour. Please try again later.';
            } else if (errorMessage.includes('maximum') && errorMessage.includes('active keys')) {
                errorMessage = 'Maximum active keys limit reached (20). Please delete some unused keys first.';
            }

            showNotification(errorMessage, 'error', 10000);
            return;
        }

        openKeySuccessModal(data.token, data.token_prefix);

    } catch (error) {
        // Close modal on network error too
        closeCreateKeyModal();
        showNotification('Network error. Please try again.', 'error');
    } finally {
        // Re-enable button and restore original text
        createBtn.disabled = false;
        createBtn.innerHTML = originalText;
    }
}

async function copyTokenToClipboard() {
    try {
        await navigator.clipboard.writeText(keyElements.tokenInput.value);
        showNotification('API key copied to clipboard', 'success');

        // Visual feedback
        const copyBtn = document.querySelector('.copy-btn');
        const originalIcon = copyBtn.querySelector('i');
        originalIcon.className = 'ti ti-check';
        setTimeout(() => {
            originalIcon.className = 'ti ti-copy';
        }, 2000);

    } catch (error) {
        showNotification('Failed to copy key', 'error');
    }
}

// Access Level Management
function setupAccessLevelHandlers() {
    const accessRadios = document.querySelectorAll('input[name="access-level"]');
    const detailedPermissions = document.getElementById('detailed-permissions');
    const accessOptions = document.querySelectorAll('.access-option');

    accessRadios.forEach(radio => {
        radio.addEventListener('change', function () {
            handleAccessLevelChange(this.value);
        });
    });

    // Initialize with current state
    const selectedLevel = document.querySelector('input[name="access-level"]:checked')?.value || 'full';
    handleAccessLevelChange(selectedLevel);
}

function handleAccessLevelChange(level) {
    const detailedPermissions = document.getElementById('detailed-permissions');
    const scopeInputs = document.querySelectorAll('.scope-input');

    if (level === 'full') {
        // Hide detailed permissions with animation
        detailedPermissions.classList.add('hidden');
    } else if (level === 'custom') {
        // Show detailed permissions with animation
        setTimeout(() => {
            detailedPermissions.classList.remove('hidden');
        }, 100);

        // Reset to only "Create URLs" selected by default
        scopeInputs.forEach(input => {
            input.checked = input.value === 'shorten:create';
        });
    }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', function () {
    fetchKeys();

    // New key button
    keyElements.newKeyBtn?.addEventListener('click', function () {
        if (checkVerificationBeforeAPIKey()) {
            openCreateKeyModal();
        }
    });

    // Create key button
    keyElements.createBtn?.addEventListener('click', createKey);

    // Access level handling
    setupAccessLevelHandlers();

    // Modal backdrop clicks
    keyElements.createModal?.addEventListener('click', (e) => {
        if (e.target === keyElements.createModal || e.target.classList.contains('modal-overlay')) {
            closeCreateKeyModal();
        }
    });

    keyElements.successModal?.addEventListener('click', (e) => {
        if (e.target === keyElements.successModal || e.target.classList.contains('modal-overlay')) {
            closeKeySuccessModal();
        }
    });

    // Delete modal wiring
    keyElements.cancelDeleteBtn?.addEventListener('click', closeDeleteModal);
    keyElements.confirmDeleteBtn?.addEventListener('click', confirmDeleteKey);
    keyElements.deleteModal?.addEventListener('click', (e) => {
        if (e.target.classList.contains('modal-container') || e.target.classList.contains('modal-backdrop')) {
            closeDeleteModal();
        }
    });

    // Escape key to close modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (keyElements.deleteModal?.classList.contains('active')) {
                closeDeleteModal();
            } else if (keyElements.createModal.style.display === 'flex') {
                closeCreateKeyModal();
            } else if (keyElements.successModal.style.display === 'flex') {
                closeKeySuccessModal();
            }
        }
    });

    // Token input click to copy
    keyElements.tokenInput?.addEventListener('click', copyTokenToClipboard);

    // Enter key in name field to create
    document.getElementById('key-name')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            createKey();
        }
    });
});

// Global functions for onclick handlers
window.closeCreateKeyModal = closeCreateKeyModal;
window.closeKeySuccessModal = closeKeySuccessModal;
window.copyTokenToClipboard = copyTokenToClipboard;