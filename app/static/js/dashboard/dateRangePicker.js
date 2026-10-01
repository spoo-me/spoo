/**
 * Advanced Date Range Picker
 * Supports relative time ranges and custom input formats
 */
class DateRangePicker {
    constructor(options = {}) {
        this.options = {
            container: options.container || 'dateRangeContainer',
            onRangeChange: options.onRangeChange || (() => {}),
            defaultRange: options.defaultRange || 'last-7-days',
            ...options
        };
        
        this.currentRange = this.options.defaultRange;
        this.customFromValue = 'now-7d';
        this.customToValue = 'now';
        this.history = [];
        
        this.init();
    }

    init() {
        this.loadHistory();
        this.render();
        this.setupEventListeners();
        this.updateRelativeSelection();
    }

    updateRelativeSelection() {
        // Update the relative options to reflect current selection
        document.querySelectorAll('.relative-option').forEach(option => {
            option.classList.toggle('selected', option.dataset.value === this.currentRange);
        });
    }

    render() {
        const container = document.getElementById(this.options.container);
        if (!container) return;

        container.innerHTML = `
            <div class="date-range-picker">
                <div class="date-range-trigger" id="dateRangeTrigger">
                    <i class="ti ti-clock clock-icon"></i>
                    <span class="selected-range">${this.formatDisplayRange()}</span>
                    <i class="ti ti-chevron-down dropdown-icon"></i>
                </div>
                
                <div class="date-range-dropdown" id="dateRangeDropdown" style="display: none;">
                    <div class="sheet-handle" aria-hidden="true"></div>
                    <div class="dropdown-content">
                        <div class="content-layout">
                            <!-- Relative Section -->
                            <div class="relative-section">
                                <h3 class="section-title">Relative</h3>
                                <div class="relative-options">
                                    ${this.renderRelativeOptions()}
                                </div>
                            </div>
                            
                            <!-- Custom Section -->
                            <div class="custom-section">
                                <h3 class="section-title">Custom</h3>
                                <div class="custom-inputs">
                                    <div class="input-row">
                                        <div class="input-group">
                                            <label>From</label>
                                            <input type="text" id="customFrom" placeholder="now-7d" value="${this.customFromValue || 'now-7d'}">
                                        </div>
                                        <div class="input-group">
                                            <label>To</label>
                                            <input type="text" id="customTo" placeholder="now" value="${this.customToValue || 'now'}">
                                        </div>
                                    </div>
                                    <button class="apply-btn" id="applyCustomRange">Apply</button>
                                </div>
                                
                                <div class="history-section">
                                    <h4>History</h4>
                                    <div class="history-list" id="historyList">
                                        ${this.renderHistory()}
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
    }

    renderRelativeOptions() {
        const options = [
            { value: 'last-30-minutes', label: 'Last 30 minutes' },
            { value: 'last-60-minutes', label: 'Last 60 minutes' },
            { value: 'last-3-hours', label: 'Last 3 hours' },
            { value: 'last-6-hours', label: 'Last 6 hours' },
            { value: 'last-12-hours', label: 'Last 12 hours' },
            { value: 'last-24-hours', label: 'Last 24 hours' },
            { value: 'last-2-days', label: 'Last 2 days' },
            { value: 'last-7-days', label: 'Last 7 days' },
            { value: 'last-14-days', label: 'Last 14 days' },
            { value: 'last-30-days', label: 'Last 30 days' },
            { value: 'everything', label: 'Everything' },
            { value: 'custom', label: 'Custom' }
        ];

        return options.map(option => `
            <div class="relative-option ${this.currentRange === option.value ? 'selected' : ''}" 
                 data-value="${option.value}">
                ${option.label}
            </div>
        `).join('');
    }

    renderHistory() {
        if (!this.history.length) {
            return '<div class="history-empty">No recent selections</div>';
        }

        return this.history.map(item => `
            <div class="history-item" data-from="${item.from}" data-to="${item.to}">
                <i class="ti ti-clock-hour-3 history-icon"></i>
                <span>${item.display}</span>
            </div>
        `).join('');
    }

    setupEventListeners() {
        const trigger = document.getElementById('dateRangeTrigger');
        const dropdown = document.getElementById('dateRangeDropdown');

        // Toggle dropdown
        trigger?.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdown.style.display === 'block';
            
            // Close all other modals if closeAllModals function exists
            if (typeof window.closeAllModals === 'function') {
                window.closeAllModals(window.dashboard);
            }
            
            // Only open if it was previously closed
            if (!isOpen) {
                dropdown.style.display = 'block';
                trigger.classList.add('active');
            }
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.date-range-picker')) {
                dropdown.style.display = 'none';
                trigger.classList.remove('active');
            }
        });

        // Swipe/tap on the bottom-sheet handle dismisses the picker.
        dropdown.addEventListener('sheet:dismiss', () => {
            dropdown.style.display = 'none';
            trigger.classList.remove('active');
        });

        // Relative options
        document.querySelectorAll('.relative-option').forEach(option => {
            option.addEventListener('click', () => {
                this.selectRelativeRange(option.dataset.value);
            });
        });

        // Custom range apply
        document.getElementById('applyCustomRange')?.addEventListener('click', () => {
            this.applyCustomRange();
        });

        // History items
        document.querySelectorAll('.history-item').forEach(item => {
            item.addEventListener('click', () => {
                this.selectHistoryItem(item.dataset.from, item.dataset.to);
            });
        });

        // Custom input validation
        const customFrom = document.getElementById('customFrom');
        const customTo = document.getElementById('customTo');
        
        customFrom?.addEventListener('input', (e) => {
            this.validateCustomInput(e.target);
        });
        
        customTo?.addEventListener('input', (e) => {
            this.validateCustomInput(e.target);
        });
    }

    selectRelativeRange(value) {
        if (value === 'custom') {
            // Focus on the custom input instead of tab switching
            const customFrom = document.getElementById('customFrom');
            if (customFrom) {
                customFrom.focus();
            }
            return;
        }

        this.currentRange = value;
        
        // Update UI
        document.querySelectorAll('.relative-option').forEach(option => {
            option.classList.toggle('selected', option.dataset.value === value);
        });
        
        // Update trigger text
        const trigger = document.querySelector('.selected-range');
        if (trigger) {
            trigger.textContent = this.formatDisplayRange();
        }
        
        // Close dropdown
        document.getElementById('dateRangeDropdown').style.display = 'none';
        document.getElementById('dateRangeTrigger').classList.remove('active');
        
        // Notify parent
        const dateRange = this.parseRelativeRange(value);
        this.options.onRangeChange(dateRange);
    }

    applyCustomRange() {
        const fromInput = document.getElementById('customFrom');
        const toInput = document.getElementById('customTo');
        
        if (!fromInput || !toInput) return;
        
        const fromValue = fromInput.value.trim();
        const toValue = toInput.value.trim();
        
        if (!fromValue || !toValue) {
            this.showError('Both From and To fields are required');
            return;
        }
        
        try {
            const dateRange = this.parseCustomRange(fromValue, toValue);
            
            // Add to history
            this.addToHistory(fromValue, toValue, `${fromValue} - ${toValue}`);
            
            // Update current values
            this.customFromValue = fromValue;
            this.customToValue = toValue;
            this.currentRange = 'custom';
            
            // Highlight the "Custom" option in relative section
            this.updateRelativeSelection();
            
            // Update trigger text
            const trigger = document.querySelector('.selected-range');
            if (trigger) {
                trigger.textContent = `${fromValue} - ${toValue}`;
            }
            
            // Close dropdown
            document.getElementById('dateRangeDropdown').style.display = 'none';
            document.getElementById('dateRangeTrigger').classList.remove('active');
            
            // Notify parent
            this.options.onRangeChange(dateRange);
            
        } catch (error) {
            this.showError(error.message);
        }
    }

    parseRelativeRange(range) {
        // All datetime calculations use UTC timezone
        const now = new Date();
        let start, end = now;

        switch (range) {
            case 'last-30-minutes':
                start = new Date(now.getTime() - 30 * 60 * 1000);
                break;
            case 'last-60-minutes':
                start = new Date(now.getTime() - 60 * 60 * 1000);
                break;
            case 'last-3-hours':
                start = new Date(now.getTime() - 3 * 60 * 60 * 1000);
                break;
            case 'last-6-hours':
                start = new Date(now.getTime() - 6 * 60 * 60 * 1000);
                break;
            case 'last-12-hours':
                start = new Date(now.getTime() - 12 * 60 * 60 * 1000);
                break;
            case 'last-24-hours':
                start = new Date(now.getTime() - 24 * 60 * 60 * 1000);
                break;
            case 'last-2-days':
                start = new Date(now.getTime() - 2 * 24 * 60 * 60 * 1000);
                break;
            case 'last-7-days':
                start = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
                break;
            case 'last-14-days':
                start = new Date(now.getTime() - 14 * 24 * 60 * 60 * 1000);
                break;
            case 'last-30-days':
                start = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
                break;
            case 'everything':
                start = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000 * 3); // Last 3 months
                break;
            default:
                start = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
        }

        return {
            start: start.toISOString(),
            end: end.toISOString(),
            range: range
        };
    }

    parseCustomRange(fromValue, toValue) {
        const fromDate = this.parseCustomInput(fromValue);
        const toDate = this.parseCustomInput(toValue);
        
        if (fromDate >= toDate) {
            throw new Error('From date must be before To date');
        }
        
        return {
            start: fromDate.toISOString(),
            end: toDate.toISOString(),
            range: 'custom'
        };
    }

    parseCustomInput(input) {
        // All datetime parsing uses UTC timezone
        input = input.trim().toLowerCase();
        
        if (input === 'now') {
            return new Date(); // UTC
        }
        
        // Parse relative formats like "now-30d", "now-2h", "now-15m", etc.
        const relativeMatch = input.match(/^now-(\d+)([dhm])$/);
        if (relativeMatch) {
            const amount = parseInt(relativeMatch[1]);
            const unit = relativeMatch[2];
            const now = new Date();
            
            switch (unit) {
                case 'd': // days
                    return new Date(now.getTime() - amount * 24 * 60 * 60 * 1000);
                case 'h': // hours
                    return new Date(now.getTime() - amount * 60 * 60 * 1000);
                case 'm': // minutes
                    return new Date(now.getTime() - amount * 60 * 1000);
                default:
                    throw new Error(`Invalid time unit: ${unit}`);
            }
        }
        
        // Try parsing as regular date
        const date = new Date(input);
        if (isNaN(date.getTime())) {
            throw new Error(`Invalid date format: ${input}. Use formats like "now", "now-30d", "now-2h", "now-15m", "2024-01-01", etc.`);
        }
        
        return date;
    }

    validateCustomInput(input) {
        try {
            this.parseCustomInput(input.value);
            input.classList.remove('invalid');
            input.classList.add('valid');
        } catch (error) {
            input.classList.remove('valid');
            input.classList.add('invalid');
        }
    }

    formatDisplayRange() {
        const rangeLabels = {
            'last-30-minutes': 'Last 30 minutes',
            'last-60-minutes': 'Last 60 minutes', 
            'last-3-hours': 'Last 3 hours',
            'last-6-hours': 'Last 6 hours',
            'last-12-hours': 'Last 12 hours',
            'last-24-hours': 'Last 24 hours',
            'last-2-days': 'Last 2 days',
            'last-7-days': 'Last 7 days',
            'last-14-days': 'Last 14 days',
            'last-30-days': 'Last 30 days',
            'everything': 'Everything',
            'custom': `${this.customFromValue || 'now-7d'} - ${this.customToValue || 'now'}`
        };
        
        return rangeLabels[this.currentRange] || 'Last 7 days';
    }

    addToHistory(from, to, display) {
        const historyItem = { from, to, display, timestamp: Date.now() };
        
        // Remove duplicates
        this.history = this.history.filter(item => 
            !(item.from === from && item.to === to)
        );
        
        // Add to beginning
        this.history.unshift(historyItem);
        
        // Keep only last 10 items
        this.history = this.history.slice(0, 10);
        
        // Save to localStorage
        this.saveHistory();
        
        // Update UI
        this.updateHistoryUI();
    }

    loadHistory() {
        try {
            const saved = localStorage.getItem('dateRangeHistory');
            if (saved) {
                const savedHistory = JSON.parse(saved);
                this.history = savedHistory.length > 0 ? savedHistory : this.getDefaultHistory();
            } else {
                this.history = this.getDefaultHistory();
            }
        } catch (error) {
            console.warn('Failed to load date range history:', error);
            this.history = this.getDefaultHistory();
        }
    }

    getDefaultHistory() {
        return [
            { from: 'now-7d', to: 'now-3d', display: 'now-7d - now-3d', timestamp: Date.now() - 86400000 },
            { from: 'now-7d', to: 'now-4d', display: 'now-7d - now-4d', timestamp: Date.now() - 172800000 },
            { from: 'now-7d', to: 'now-1d', display: 'now-7d - now-1d', timestamp: Date.now() - 259200000 },
            { from: 'now-30d', to: 'now-1d', display: 'now-30d - now-1d', timestamp: Date.now() - 345600000 },
            { from: 'now-30d', to: 'now-5d', display: 'now-30d - now-5d', timestamp: Date.now() - 432000000 },
            { from: 'now-30d', to: 'now-10d', display: 'now-30d - now-10d', timestamp: Date.now() - 518400000 },
            { from: 'now-30d', to: 'now-20d', display: 'now-30d - now-20d', timestamp: Date.now() - 604800000 }
        ];
    }

    saveHistory() {
        try {
            localStorage.setItem('dateRangeHistory', JSON.stringify(this.history));
        } catch (error) {
            console.warn('Failed to save date range history:', error);
        }
    }

    updateHistoryUI() {
        const historyList = document.getElementById('historyList');
        if (historyList) {
            historyList.innerHTML = this.renderHistory();
            
            // Re-attach event listeners
            document.querySelectorAll('.history-item').forEach(item => {
                item.addEventListener('click', () => {
                    this.selectHistoryItem(item.dataset.from, item.dataset.to);
                });
            });
        }
    }

    selectHistoryItem(from, to) {
        document.getElementById('customFrom').value = from;
        document.getElementById('customTo').value = to;
        this.applyCustomRange();
    }

    showError(message) {
        // Simple error display - can be enhanced with toast notifications
        alert(message);
    }

    getCurrentRange() {
        if (this.currentRange === 'custom') {
            return this.parseCustomRange(this.customFromValue, this.customToValue);
        } else {
            return this.parseRelativeRange(this.currentRange);
        }
    }

    setRange(range) {
        this.currentRange = range;
        this.render();
        // Reattach event listeners after DOM is recreated
        this.setupEventListeners();
        // Refresh UI selection state
        this.updateRelativeSelection();
    }
}

// Export for use in other modules
window.DateRangePicker = DateRangePicker;