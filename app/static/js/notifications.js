(function () {
    const ICONS = {
        success: 'ti ti-check',
        error: 'ti ti-alert-triangle',
        warning: 'ti ti-alert-circle',
        info: 'ti ti-info-circle'
    };

    window.showNotification = function (message, type, duration) {
        type = type || 'info';
        duration = duration || 4000;

        // Remove existing notifications
        document.querySelectorAll('.notification').forEach(function (n) {
            n.remove();
        });

        var notification = document.createElement('div');
        notification.className = 'notification notification-' + type;

        var icon = document.createElement('i');
        icon.className = ICONS[type] || ICONS.info;

        var text = document.createElement('span');
        text.textContent = message;

        var progress = document.createElement('div');
        progress.className = 'notification-progress';
        progress.style.animationDuration = duration + 'ms';

        notification.appendChild(icon);
        notification.appendChild(text);
        notification.appendChild(progress);
        document.body.appendChild(notification);

        // Slide in
        setTimeout(function () {
            notification.classList.add('show');
        }, 50);

        // Slide out and remove
        setTimeout(function () {
            notification.classList.remove('show');
            setTimeout(function () {
                if (notification.parentNode) {
                    notification.remove();
                }
            }, 300);
        }, duration);
    };
})();
