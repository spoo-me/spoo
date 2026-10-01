/**
 * Smart Datetime Formatter using Day.js
 */
(function () {
    // Initialize day.js plugins
    if (typeof dayjs !== 'undefined') {
        dayjs.extend(dayjs_plugin_relativeTime);
    }

    // Vanilla JS helpers to replace the three plugins
    function isToday(dayjsObj) {
        const today = dayjs();
        return dayjsObj.format('YYYY-MM-DD') === today.format('YYYY-MM-DD');
    }

    function isYesterday(dayjsObj) {
        const yesterday = dayjs().subtract(1, 'day');
        return dayjsObj.format('YYYY-MM-DD') === yesterday.format('YYYY-MM-DD');
    }

    function isSameOrAfter(dayjsObj, compareObj, unit) {
        return dayjsObj.valueOf() >= compareObj.valueOf();
    }

    function smartFormat(dateInput, showRelativeToday = false) {
        if (!dateInput) return '—';

        try {
            // Handle different input types
            let d;
            if (typeof dateInput === 'number') {
                d = dateInput > 10000000000 ? dayjs(dateInput) : dayjs(dateInput * 1000);
            } else {
                d = dayjs(dateInput);
            }

            if (!d.isValid()) return '—';

            const now = dayjs();

            // If today and showRelativeToday enabled
            if (isToday(d) && showRelativeToday) {
                const hoursAgo = now.diff(d, 'hour');
                if (hoursAgo < 1) return d.fromNow();
                return `${hoursAgo}h ago`;
            }

            // Standard formats
            if (isToday(d)) return `Today at ${d.format('h:mm A')}`;
            if (isYesterday(d)) return `Yesterday at ${d.format('h:mm A')}`;
            if (isSameOrAfter(d, now.subtract(7, 'day'), 'day')) return d.format('ddd, h:mm A');
            if (d.year() === now.year()) return d.format('D MMM, h:mm A');
            return d.format('D MMM YYYY, h:mm A');

        } catch (error) {
            return '—';
        }
    }

    /**
     * Format for created dates (less relative)
     */
    function formatCreated(dateInput) {
        return smartFormat(dateInput, false);
    }

    /**
     * Format for last click dates (more relative)
     */
    function formatLastClick(dateInput) {
        return smartFormat(dateInput, true);
    }

    // Export functions
    window.SmartDatetime = {
        formatCreated: formatCreated,
        formatLastClick: formatLastClick
    };

    // Backward compatibility
    window.formatDate = formatCreated;
    window.formatTs = formatLastClick;
})();