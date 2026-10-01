window.addEventListener('scroll', function () {
    var navbar = document.querySelector('.navbar');
    var mobileNavbar = document.querySelector('.mobile-navbar');
    if (window.pageYOffset > 30) {
        navbar.classList.add('scrolled');
        mobileNavbar.classList.add('scrolled');
    } else {
        navbar.classList.remove('scrolled');
        mobileNavbar.classList.remove('scrolled');
    }
});

// Fetch GitHub stars
async function fetchGitHubStars() {
    try {
        const response = await fetch('/metric');
        if (response.ok) {
            const data = await response.json();
            const stars = data['github-stars'];
            if (stars > 0) {
                const formatted = stars >= 1000 ? (stars / 1000).toFixed(1) + 'k' : stars;
                const starEl = document.getElementById('github-star-count');
                if (starEl) {
                    starEl.textContent = formatted;
                }
            }
        }
    } catch (error) {
        // swallow errors silently (do not log)
    }
}

// Mobile menu toggle
function setupMobileMenu() {
    const burger = document.querySelector('.burger');
    const mobileNavbar = document.querySelector('.mobile-navbar');
    const menu = document.querySelector('.mobile-menu');

    if (burger && menu && mobileNavbar) {
        burger.addEventListener('click', function (e) {
            e.stopPropagation();
            const isExpanded = burger.getAttribute('aria-expanded') === 'true';
            burger.setAttribute('aria-expanded', String(!isExpanded));

            // Toggle class on navbar to control menu visibility
            if (!isExpanded) {
                mobileNavbar.classList.add('menu-open');
            } else {
                mobileNavbar.classList.remove('menu-open');
            }
        });

        // Close menu when clicking outside
        document.addEventListener('click', function (e) {
            if (!burger.contains(e.target) && !menu.contains(e.target)) {
                burger.setAttribute('aria-expanded', 'false');
                mobileNavbar.classList.remove('menu-open');
            }
        });

        // Close menu when clicking a link
        menu.querySelectorAll('a').forEach(link => {
            link.addEventListener('click', function () {
                burger.setAttribute('aria-expanded', 'false');
                mobileNavbar.classList.remove('menu-open');
            });
        });
    }
}

// Profile dropdown toggles
document.addEventListener('DOMContentLoaded', function () {
    setupMobileMenu();

    function setupProfileMenu(buttonId, dropdownId) {
        var btn = document.getElementById(buttonId);
        var dd = document.getElementById(dropdownId);
        if (!btn || !dd) return;
        function toggle() {
            var open = dd.style.display === 'block';
            dd.style.display = open ? 'none' : 'block';
            btn.setAttribute('aria-expanded', String(!open));
        }
        btn.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
        document.addEventListener('click', function (e) {
            if (dd.contains(e.target) || btn.contains(e.target)) return;
            dd.style.display = 'none';
            btn.setAttribute('aria-expanded', 'false');
        });
    }
    setupProfileMenu('profileButton', 'profileDropdown');
    setupProfileMenu('mProfileButton', 'mProfileDropdown');
    // Fetch GitHub stars after UI initialization so failures don't interfere
    // with profile/menu setup.
    fetchGitHubStars();
    // Add delegated handlers so profile buttons still work if elements are
    // replaced or re-rendered later (avoids relying on attached listeners).
    try {
        document.addEventListener('click', function (e) {
            // Desktop
            const btn = document.getElementById('profileButton');
            const dd = document.getElementById('profileDropdown');
            if (btn && dd) {
                if (btn.contains(e.target)) {
                    e.stopPropagation();
                    const open = dd.style.display === 'block';
                    dd.style.display = open ? 'none' : 'block';
                    btn.setAttribute('aria-expanded', String(!open));
                    return;
                }
                if (!dd.contains(e.target)) {
                    dd.style.display = 'none';
                    btn.setAttribute('aria-expanded', 'false');
                }
            }

            // Mobile
            const mBtn = document.getElementById('mProfileButton');
            const mDd = document.getElementById('mProfileDropdown');
            if (mBtn && mDd) {
                if (mBtn.contains(e.target)) {
                    e.stopPropagation();
                    const open = mDd.style.display === 'block';
                    mDd.style.display = open ? 'none' : 'block';
                    mBtn.setAttribute('aria-expanded', String(!open));
                    return;
                }
                if (!mDd.contains(e.target)) {
                    mDd.style.display = 'none';
                    mBtn.setAttribute('aria-expanded', 'false');
                }
            }
        });
    } catch (e) {
        // swallow errors silently (do not log)
    }
});

const closeButton = document.querySelector('.self-promo-close');

if (closeButton) {
    closeButton.addEventListener('click', closeSelfPromo);
}

function closeSelfPromo() {
    const promo = document.querySelector('.self-promo');
    if (promo) {
        promo.classList.add('hidden');
    }
}