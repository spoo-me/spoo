(function () {
    function getField(fieldId) {
        var input = document.getElementById(fieldId);
        if (!input) return null;
        return input.closest('.field');
    }

    function refreshOwnerTabs(field) {
        if (!field || !window.ModalTabs || !window.ModalTabs.refreshErrors) return;
        var modal = field.closest('.modal');
        if (modal) window.ModalTabs.refreshErrors(modal);
    }

    window.setFieldError = function (fieldId, message) {
        var field = getField(fieldId);
        if (!field) return;

        if (!message) {
            window.clearFieldError(fieldId);
            return;
        }

        field.classList.add('has-error');
        var existing = field.querySelector(':scope > .field-error');
        if (existing) {
            existing.textContent = message;
        } else {
            var el = document.createElement('small');
            el.className = 'field-error';
            el.textContent = message;
            field.appendChild(el);
        }
        refreshOwnerTabs(field);
    };

    window.clearFieldError = function (fieldId) {
        var field = getField(fieldId);
        if (!field) return;
        field.classList.remove('has-error');
        var existing = field.querySelector(':scope > .field-error');
        if (existing) existing.remove();
        refreshOwnerTabs(field);
    };

    window.clearFormErrors = function (container) {
        if (!container) return;
        container.querySelectorAll('.field.has-error').forEach(function (f) {
            f.classList.remove('has-error');
        });
        container.querySelectorAll('.field-error').forEach(function (e) {
            e.remove();
        });
        var modal = container.closest ? container.closest('.modal') : null;
        if (modal && window.ModalTabs && window.ModalTabs.refreshErrors) {
            window.ModalTabs.refreshErrors(modal);
        }
    };

    /**
     * Map Pydantic / FastAPI validator text to human copy.
     * "alias: String should have at least 3 characters" -> "Must be at least 3 characters"
     */
    window.humanizeApiError = function (rawMessage) {
        if (!rawMessage) return '';
        var msg = String(rawMessage).replace(/^[a-z_][a-z0-9_]*:\s*/i, '');
        msg = msg
            .replace(/^String should have at least (\d+) character(s)?$/i, 'Must be at least $1 character$2')
            .replace(/^String should have at most (\d+) character(s)?$/i, 'Must be at most $1 character$2')
            .replace(/^String should match pattern.*$/i, 'Contains invalid characters')
            .replace(/^Value error,\s*/i, '')
            .replace(/^Input should be.*$/i, 'Invalid value');
        if (msg.length > 0) msg = msg.charAt(0).toUpperCase() + msg.slice(1);
        return msg;
    };

    /**
     * Apply a server validation error to a form.
     * Handles { field, error } and { details: [{ field, error }, ...] }.
     * fieldMap maps backend field name -> frontend input id.
     * Returns the number of field errors successfully applied.
     */
    window.applyServerErrors = function (container, apiResponse, fieldMap) {
        if (!apiResponse || !fieldMap) return 0;
        var entries = [];
        if (Array.isArray(apiResponse.details) && apiResponse.details.length) {
            entries = apiResponse.details;
        } else if (apiResponse.field) {
            entries = [{ field: apiResponse.field, error: apiResponse.error }];
        }
        var applied = 0;
        entries.forEach(function (e) {
            var id = fieldMap[e.field];
            if (!id) return;
            window.setFieldError(id, window.humanizeApiError(e.error));
            applied++;
        });
        return applied;
    };
})();
