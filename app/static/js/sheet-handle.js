(function () {
    const CLOSE_DISTANCE = 80;
    const CLOSE_VELOCITY = 0.4;

    function getSheet(handle) {
        return handle.closest('[data-dropdown-menu], .dropdown-menu')
            || handle.parentElement;
    }

    let active = null; // { handle, sheet, dropdown, startY, startTime, delta, moved, pointerId }

    function begin(handle, clientY, pointerId) {
        const sheet = getSheet(handle);
        if (!sheet) return false;
        active = {
            handle,
            sheet,
            dropdown: handle.closest('.dropdown'),
            startY: clientY,
            startTime: Date.now(),
            delta: 0,
            moved: false,
            pointerId,
        };
        sheet.style.transition = 'none';
        return true;
    }

    function move(clientY) {
        if (!active) return;
        const delta = Math.max(0, clientY - active.startY);
        active.delta = delta;
        if (delta > 2) active.moved = true;
        active.sheet.style.transform = `translateY(${delta}px)`;
    }

    function end() {
        if (!active) return;
        const { sheet, dropdown, delta, startTime, moved } = active;
        const duration = Math.max(1, Date.now() - startTime);
        const velocity = delta / duration;

        sheet.style.transition = '';
        sheet.style.transform = '';

        const shouldClose =
            !moved || delta > CLOSE_DISTANCE || velocity > CLOSE_VELOCITY;

        if (shouldClose) {
            sheet.dispatchEvent(new CustomEvent('sheet:dismiss', { bubbles: true }));
            if (dropdown && window.Dropdown) {
                window.Dropdown.close(dropdown);
            }
        }

        active = null;
    }

    document.addEventListener('touchstart', (e) => {
        const handle = e.target.closest('.sheet-handle');
        if (!handle) return;
        begin(handle, e.touches[0].clientY, null);
    }, { passive: true });

    document.addEventListener('touchmove', (e) => {
        if (!active) return;
        move(e.touches[0].clientY);
        if (e.cancelable) e.preventDefault();
    }, { passive: false });

    document.addEventListener('touchend', () => end());
    document.addEventListener('touchcancel', () => end());

    document.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'touch') return;
        const handle = e.target.closest('.sheet-handle');
        if (!handle) return;
        if (begin(handle, e.clientY, e.pointerId)) {
            handle.setPointerCapture(e.pointerId);
        }
    });

    document.addEventListener('pointermove', (e) => {
        if (!active || e.pointerType === 'touch') return;
        move(e.clientY);
    });

    document.addEventListener('pointerup', (e) => {
        if (!active || e.pointerType === 'touch') return;
        end();
    });

    document.addEventListener('pointercancel', (e) => {
        if (!active || e.pointerType === 'touch') return;
        end();
    });
})();
