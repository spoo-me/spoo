function isEmailVerified() {
    if (typeof g !== 'undefined' && g.jwt_claims && typeof g.jwt_claims.email_verified === 'boolean') {
        return g.jwt_claims.email_verified;
    }
    // Fail open if claims unavailable
    return true;
}

function showVerificationModal(action = "perform this action") {
    const modalHTML = `
        <div id="verificationModal" class="email-verification-modal">
            <div class="email-verification-backdrop"></div>
            <div class="email-verification-container">
                <div class="email-verification-content">
                    <div class="email-verification-header">
                        <div class="email-verification-title-section">
                            <div class="email-verification-icon">
                                <i class="ti ti-mail"></i>
                            </div>
                            <div>
                                <h2 class="email-verification-title">Email Verification Required</h2>
                                <p class="email-verification-subtitle">Verify your email to continue</p>
                            </div>
                        </div>
                    </div>

                    <div class="email-verification-body">
                        <div class="email-verification-text">
                            <p>You need to verify your email address to ${action}.</p>
                            <p>We've sent a verification code to your email. Please check your inbox and verify your account.</p>
                        </div>
                    </div>

                    <div class="email-verification-footer">
                        <button type="button" class="email-verification-btn email-verification-btn-secondary" onclick="closeVerificationModal()">
                            <span>Cancel</span>
                        </button>
                        <a href="/auth/verify" class="email-verification-btn email-verification-btn-primary">
                            <span>Verify Email</span>
                        </a>
                    </div>
                </div>
            </div>
        </div>
    `;

    const existingModal = document.getElementById('verificationModal');
    if (existingModal) {
        existingModal.remove();
    }

    document.body.insertAdjacentHTML('beforeend', modalHTML);

    requestAnimationFrame(() => {
        const modal = document.getElementById('verificationModal');
        if (modal) {
            requestAnimationFrame(() => {
                modal.classList.add('active');
            });
        }
    });

    if (!document.getElementById('verificationModalStyles')) {
        const styles = document.createElement('style');
        styles.id = 'verificationModalStyles';
        styles.textContent = `
            .email-verification-modal {
                display: none;
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                z-index: 99999;
                opacity: 0;
                visibility: hidden;
                transition: opacity 0.4s cubic-bezier(0.4, 0, 0.2, 1), visibility 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            }

            .email-verification-modal.active {
                display: flex;
                opacity: 1;
                visibility: visible;
            }

            .email-verification-backdrop {
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0, 0, 0, 0.7);
                backdrop-filter: blur(10px) saturate(180%) brightness(0.7);
                -webkit-backdrop-filter: blur(10px) saturate(180%) brightness(0.7);
            }

            .email-verification-container {
                display: flex;
                align-items: center;
                justify-content: center;
                width: 100%;
                height: 100%;
                padding: 20px;
                position: relative;
                z-index: 2;
            }

            .email-verification-content {
                backdrop-filter: blur(60px);
                -webkit-backdrop-filter: blur(60px);
                background: rgba(15, 20, 35, 0.5);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 16px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5), 0 0 0 1px rgba(255, 255, 255, 0.05);
                width: 100%;
                max-width: 500px;
                overflow: hidden;
                transform: scale(0.85) translateY(20px);
                opacity: 0;
                transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            }

            .email-verification-modal.active .email-verification-content {
                transform: scale(1) translateY(0);
                opacity: 1;
            }

            .email-verification-header {
                padding: 20px 24px 16px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                background: rgba(255, 255, 255, 0.02);
            }

            .email-verification-title-section {
                display: flex;
                align-items: center;
                gap: 16px;
            }

            .email-verification-icon {
                width: 48px;
                height: 48px;
                border-radius: 12px;
                display: flex;
                align-items: center;
                justify-content: center;
                font-size: 24px;
                flex-shrink: 0;
                background: linear-gradient(135deg, #7c3aed, #6d28d9);
                color: white;
            }

            .email-verification-title {
                font-size: 24px;
                font-weight: 600;
                color: var(--text-primary, #ffffff);
                margin: 0 0 4px 0;
                line-height: 1.2;
            }

            .email-verification-subtitle {
                font-size: 14px;
                color: var(--text-secondary, rgba(255, 255, 255, 0.6));
                margin: 0;
                line-height: 1.4;
            }

            .email-verification-body {
                padding: 24px;
            }

            .email-verification-text p {
                color: var(--text-secondary, rgba(255, 255, 255, 0.7));
                line-height: 1.6;
                margin: 0 0 16px 0;
                font-size: 14px;
            }

            .email-verification-text p:last-child {
                margin-bottom: 0;
            }

            .email-verification-footer {
                display: flex;
                justify-content: flex-end;
                gap: 12px;
                padding: 16px 24px;
                border-top: 1px solid rgba(255, 255, 255, 0.1);
                background: rgba(255, 255, 255, 0.02);
            }

            .email-verification-btn {
                padding: 10px 20px;
                border-radius: 8px;
                font-size: 14px;
                font-weight: 500;
                cursor: pointer;
                transition: all 0.2s ease;
                display: inline-flex;
                align-items: center;
                gap: 8px;
                border: none;
                text-decoration: none;
                font-family: inherit;
            }

            .email-verification-btn-secondary {
                background: rgba(255, 255, 255, 0.08);
                color: var(--text-primary, #ffffff);
                border: 1px solid rgba(255, 255, 255, 0.1);
            }

            .email-verification-btn-secondary:hover {
                background: rgba(255, 255, 255, 0.12);
                border-color: rgba(255, 255, 255, 0.2);
            }

            .email-verification-btn-primary {
                background: #7c3aed;
                color: white;
                border: 1px solid #7c3aed;
            }

            .email-verification-btn-primary:hover {
                background: #6d28d9;
                border-color: #6d28d9;
            }

            @media (max-width: 768px) {
                .email-verification-content {
                    max-width: 95vw;
                    margin: 20px;
                }

                .email-verification-title {
                    font-size: 20px;
                }

                .email-verification-icon {
                    width: 40px;
                    height: 40px;
                    font-size: 20px;
                }
            }
        `;
        document.head.appendChild(styles);
    }
}

function closeVerificationModal() {
    const modal = document.getElementById('verificationModal');
    if (modal) {
        modal.classList.remove('active');
        setTimeout(() => modal.remove(), 400);
    }
}

function checkVerificationBeforeShorten() {
    if (!isEmailVerified()) {
        showVerificationModal("create short URLs");
        return false;
    }
    return true;
}

function checkVerificationBeforeAPIKey() {
    if (!isEmailVerified()) {
        showVerificationModal("create API keys");
        return false;
    }
    return true;
}

function checkVerification(action = "perform this action") {
    if (!isEmailVerified()) {
        showVerificationModal(action);
        return false;
    }
    return true;
}

document.addEventListener('click', function (e) {
    if (e.target.classList.contains('email-verification-backdrop')) {
        closeVerificationModal();
    }
});

document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
        closeVerificationModal();
    }
});
