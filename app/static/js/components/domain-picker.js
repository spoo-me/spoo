// Domain picker — behavior layer on top of the `.dropdown` primitive.
//
// Markup lives in `templates/partials/domain_picker.html`; this file binds
// `dropdown:select` events to:
//   1) update the visible label in the trigger
//   2) sync the hidden input's value (so form submission picks it up)
//   3) fire a `change` event on the hidden input (forms can listen)
//   4) fire a `domain-picker:change` event on the wrapper (filter chips
//      can listen without poking at the hidden input)
//
// `[data-domain-picker]` is the wrapper. Wrapper carries `data-mode` =
// "select" | "filter". In filter mode, the sentinel value "" means "no
// filter" and the trigger displays "All domains".
//
// Programmatic API — useful from links.js when the user resets filters,
// opens the create modal, etc.:
//   window.DomainPicker.setValue(wrapperEl, value)   // updates label + input + fires events
//   window.DomainPicker.getValue(wrapperEl)          // returns current value
(function () {
    'use strict';

    function _hiddenInput(wrapper) {
        return wrapper.querySelector('input[type="hidden"]');
    }

    function _labelEl(wrapper) {
        return wrapper.querySelector('[data-picker-value]');
    }

    function _labelFor(value, mode, wrapper) {
        const display = wrapper.getAttribute('data-display') || 'picker';
        // Filter sentinel = "All domains" trigger copy.
        if (mode === 'filter' && (value === '' || value == null)) {
            return 'All domains';
        }
        let fqdn = value;
        if (!fqdn) {
            // Fallback for selector mode with no preselected value — pull
            // the first item's text so we never show a blank trigger.
            const first = wrapper.querySelector('.domain-picker-item[data-value]');
            fqdn = first ? first.getAttribute('data-value') : '';
        }
        // Prefix display: show the full https://<fqdn>/ so users see exactly
        // what their final short URL becomes.
        if (display === 'prefix' && fqdn) {
            return 'https://' + fqdn + '/';
        }
        return fqdn;
    }

    function _markSelected(wrapper, value) {
        wrapper.querySelectorAll('.domain-picker-item').forEach(function (item) {
            const itemVal = item.getAttribute('data-value');
            // null guard because the "Add a custom domain" CTA is an <a>
            // without data-value; we leave its highlight state alone.
            if (itemVal === null) return;
            const isMatch = itemVal === value;
            item.classList.toggle('is-selected', isMatch);
            if (isMatch) {
                item.setAttribute('aria-selected', 'true');
            } else {
                item.removeAttribute('aria-selected');
            }
        });
    }

    function setValue(wrapper, value, options) {
        if (!wrapper) return;
        const mode = wrapper.getAttribute('data-mode') || 'select';
        const input = _hiddenInput(wrapper);
        const label = _labelEl(wrapper);
        const normalized = value == null ? '' : String(value);
        const prevValue = input ? input.value : '';

        if (input) input.value = normalized;
        if (label) label.textContent = _labelFor(normalized, mode, wrapper);
        _markSelected(wrapper, normalized);

        // Don't fire events when we're seeding the initial value or the
        // value hasn't actually changed — prevents form-listeners from
        // running on page load.
        if (prevValue === normalized) return;
        if (options && options.silent) return;

        if (input) {
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }
        wrapper.dispatchEvent(new CustomEvent('domain-picker:change', {
            bubbles: true,
            detail: { value: normalized, previous: prevValue, mode: mode },
        }));
    }

    function getValue(wrapper) {
        const input = _hiddenInput(wrapper);
        return input ? input.value : '';
    }

    function _onDropdownSelect(e) {
        const wrapper = e.target.closest('[data-domain-picker]');
        if (!wrapper) return;
        // The "Add a custom domain" link is an <a> the dropdown primitive
        // doesn't have a data-value for — let the anchor's default href
        // navigate naturally and don't try to set anything.
        const item = e.detail && e.detail.item;
        if (!item || item.getAttribute('data-value') === null) return;
        setValue(wrapper, e.detail.value);
    }

    function init() {
        // The `.dropdown` primitive fires `dropdown:select` on `<dropdown>`,
        // which bubbles. One delegated listener handles every picker on the
        // page (modal + filter + future surfaces).
        document.addEventListener('dropdown:select', _onDropdownSelect);

        // Seed selected state on page-load values (server may have rendered
        // a value via the macro's `value=` arg). No event fired — silent seed.
        document.querySelectorAll('[data-domain-picker]').forEach(function (w) {
            const input = _hiddenInput(w);
            if (!input) return;
            _markSelected(w, input.value);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Expose the imperative API on `window.DomainPicker`. Kept tiny so a
    // future bundler step doesn't have to inventory a sprawling export.
    window.DomainPicker = { setValue: setValue, getValue: getValue };
})();
