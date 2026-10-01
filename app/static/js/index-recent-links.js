// Reusable handler for copy button clicks
function handleCopyClick(button) {
    const url = button.getAttribute('data-url');

    // Create a temporary input element to copy the URL
    const tempInput = document.createElement('input');
    tempInput.value = url;
    document.body.appendChild(tempInput);

    // Select the URL in the input element
    tempInput.select();
    tempInput.setSelectionRange(0, 99999); // For mobile devices

    // Copy the URL to the clipboard
    document.execCommand('copy');

    // Remove the temporary input element
    document.body.removeChild(tempInput);

    // Provide visual feedback to indicate successful copying
    button.innerText = 'Copied!';
    setTimeout(() => {
        button.innerText = 'Copy';
    }, 1000);
}

// Reusable handler for stats button clicks
function handleStatsClick(button) {
    const href = button.parentNode.parentNode.parentNode.querySelector('.short-url a').getAttribute('href');
    const alias = href.replace(/^\//, '');
    window.location.href = `/stats/${alias}`;
}

// Handler for edit button clicks
function handleEditClick(button) {
    const alias = button.getAttribute('data-alias');
    if (typeof openLandingEditModal === 'function') {
        openLandingEditModal(alias);
    }
}

// Single document-level event delegation for copy, stats, and edit buttons
document.addEventListener('click', (e) => {
    // Handle copy button clicks
    if (e.target.classList.contains('copy-button')) {
        handleCopyClick(e.target);
    }
    // Handle stats button clicks
    else if (e.target.classList.contains('stats-button')) {
        handleStatsClick(e.target);
    }
    // Handle edit button clicks
    else if (e.target.classList.contains('edit-button')) {
        handleEditClick(e.target);
    }
});

// Render recent URLs without QR codes
function renderRecentURLs() {
    const container = document.getElementById('recentURLs');
    if (!container) return;

    // Don't show recent URLs for signed-up users
    if (window.isLoggedIn === true) {
        container.innerHTML = '';
        return;
    }

    let list = [];
    try { list = JSON.parse(localStorage.getItem('recentURLs')) || []; } catch (_) { list = []; }
    container.innerHTML = '';

    list.forEach((alias) => {
        const shortUrl = `${window.location.origin}/${alias}`;

        const wrapper = document.createElement('div');
        wrapper.className = 'url-container';
        wrapper.innerHTML = `
            <div class="section-1">
                <div class="left-section">
                    <span class="short-url">
                        <a href="/${alias}" target="_blank">${shortUrl.replace(/^https?:\/\//, '')}</a>
                    </span>
                </div>
            </div>
            <div class="section-2">
                <div class="button-container">
                    <button class="copy-button" data-url="${shortUrl}">Copy</button>
                    <button class="edit-button" data-alias="${alias}">Edit</button>
                    <button class="stats-button">Stats</button>
                </div>
            </div>
        `;
        container.appendChild(wrapper);
    });
}

document.addEventListener('DOMContentLoaded', renderRecentURLs);

// Re-render when authentication state changes
document.addEventListener('auth:init', renderRecentURLs);