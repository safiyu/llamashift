
// Default runtime parameters
const DEFAULT_PARAMS = {
    ctxSize: 16384,
    nParallel: 1,
    nGpuLayers: 99,
    temperature: 0.7,
    maxTokens: 4096,
    topP: 0.9,
    batchSize: 2048,
    threads: 0,
};

// State
let appState = {
    models: [],
    host: {},
    gpus: { nvidia: null, amd: null },
    activeLogModel: null,
    logPollIntervalId: null,
    chatModelId: null,
    chatHistory: [],
    isInitialized: false,
    serverMode: 'single_port'
};

// Pending model ID for single-port confirmation modal
let pendingModelId = null;

// SVG Circle circumference for gauges: 2 * Math.PI * r(40) = 251
const GAUGE_CIRCUMFERENCE = 251;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initUI();
    initPinSecurity(); // Initialize PIN security system
    fetchSystemState(true); // first quick fetch

    // Set up continuous state polling (every 2 seconds)
    setInterval(() => {
        fetchSystemState(false);
    }, 2000);

    // Set up real-time clocks
    setInterval(updateClock, 1000);
});

function initUI() {
    // Header actions
    document.getElementById('btn-refresh').addEventListener('click', () => {
        fetchSystemState(true);
    });

    document.getElementById('btn-stop-all').addEventListener('click', handleStopAll);

    // Add model button -> open new model overlay
    const btnAddModel = document.getElementById('btn-add-model');
    if (btnAddModel) {
        btnAddModel.addEventListener('click', () => {
            openModal('new-model-overlay');
        });
    }

    // Restart service button
    const btnRestartService = document.getElementById('btn-restart-service');
    if (btnRestartService) {
        btnRestartService.addEventListener('click', () => {
            openModal('restart-confirm-overlay');
        });
    }

    // Start all models button
    const btnStartAll = document.getElementById('btn-start-all');
    if (btnStartAll) {
        btnStartAll.addEventListener('click', handleStartAll);
    }

    // Config modal button - note: btn-config-modal may not exist in HTML, but handle if added
    const btnConfigModal = document.getElementById('btn-config-modal');
    if (btnConfigModal) {
        btnConfigModal.addEventListener('click', () => {
            openModelConfigModal();
        });
    }

    // Console controls
    document.getElementById('btn-close-console').addEventListener('click', closeConsole);

    // New model overlay buttons
    const btnCreateModel = document.getElementById('btn-create-model');
    if (btnCreateModel) {
        btnCreateModel.addEventListener('click', handleCreateModel);
    }

    const btnCloseNewModel = document.getElementById('btn-close-new-model');
    if (btnCloseNewModel) {
        btnCloseNewModel.addEventListener('click', () => {
            closeModal('new-model-overlay');
        });
    }

    // Restart overlay buttons
    const btnConfirmRestart = document.getElementById('btn-confirm-restart');
    if (btnConfirmRestart) {
        btnConfirmRestart.addEventListener('click', handleRestartService);
    }

    const btnCloseRestart = document.getElementById('btn-close-restart');
    const btnCloseRestartModal = document.getElementById('btn-close-restart-modal');
    const btnCancelRestart = document.getElementById('btn-cancel-restart');
    if (btnCloseRestart) {
        btnCloseRestart.addEventListener('click', () => {
            closeModal('restart-confirm-overlay');
        });
    }
    if (btnCloseRestartModal) {
        btnCloseRestartModal.addEventListener('click', () => {
            closeModal('restart-confirm-overlay');
        });
    }
    if (btnCancelRestart) {
        btnCancelRestart.addEventListener('click', () => {
            closeModal('restart-confirm-overlay');
        });
    }

    // Single port confirmation modal buttons
    if (document.getElementById('btn-close-single-port-confirm')) {
        document.getElementById('btn-close-single-port-confirm').addEventListener('click', () => {
            closeModal('single-port-confirm-overlay');
            pendingModelId = null;
        });
    }
    if (document.getElementById('btn-cancel-single-port')) {
        document.getElementById('btn-cancel-single-port').addEventListener('click', () => {
            closeModal('single-port-confirm-overlay');
            pendingModelId = null;
        });
    }
    if (document.getElementById('btn-confirm-single-port')) {
        document.getElementById('btn-confirm-single-port').addEventListener('click', () => {
            closeModal('single-port-confirm-overlay');
            if (pendingModelId) {
                executeModelToggle(pendingModelId);
                pendingModelId = null;
            }
        });
    }

    // Config modal buttons
    const btnCloseConfigModal = document.getElementById('btn-close-config-modal');
    if (btnCloseConfigModal) {
        btnCloseConfigModal.addEventListener('click', () => {
            closeModal('model-config-overlay');
        });
    }

    const btnSaveConfig = document.getElementById('btn-save-config');
    if (btnSaveConfig) {
        btnSaveConfig.addEventListener('click', handleSaveConfig);
    }

    const btnResetConfig = document.getElementById('btn-reset-config');
    if (btnResetConfig) {
        btnResetConfig.addEventListener('click', handleResetConfig);
    }

    // Playground controls
    document.getElementById('btn-playground-toggle').addEventListener('click', togglePlayground);
    document.getElementById('btn-close-playground').addEventListener('click', closePlayground);
    document.getElementById('chat-model-select').addEventListener('change', handleChatModelChange);
    document.getElementById('btn-send-chat').addEventListener('click', handleSendChat);
    document.getElementById('chat-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendChat();
        }
    });

    // Toast close button
    const btnCloseToast = document.getElementById('btn-close-toast');
    if (btnCloseToast) {
        btnCloseToast.addEventListener('click', () => {
            const toast = document.getElementById('toast-container');
            if (toast) toast.classList.remove('show');
        });
    }

    // Export models button
    const btnExportModels = document.getElementById('btn-export-models');
    if (btnExportModels) {
        btnExportModels.addEventListener('click', handleExportModels);
    }

    // Import models button
    const btnImportModels = document.getElementById('btn-import-models');
    if (btnImportModels) {
        btnImportModels.addEventListener('click', () => openModal('import-models-overlay'));
    }

    // Import modal buttons
    const btnCloseImportModels = document.getElementById('btn-close-import-models');
    if (btnCloseImportModels) {
        btnCloseImportModels.addEventListener('click', () => closeModal('import-models-overlay'));
    }

    const btnCancelImport = document.getElementById('btn-cancel-import');
    if (btnCancelImport) {
        btnCancelImport.addEventListener('click', () => closeModal('import-models-overlay'));
    }

    const btnSelectImportFile = document.getElementById('btn-select-import-file');
    if (btnSelectImportFile) {
        btnSelectImportFile.addEventListener('click', () => {
            document.getElementById('import-file-input').click();
        });
    }

    const importFileInput = document.getElementById('import-file-input');
    if (importFileInput) {
        importFileInput.addEventListener('change', handleImportFileSelect);
    }

    const btnConfirmImport = document.getElementById('btn-confirm-import');
    if (btnConfirmImport) {
        btnConfirmImport.addEventListener('click', handleImportModels);
    }
}

/**
 * Close a modal by ID
 */
function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.remove('open');
    }
}

/**
 * Open a modal by ID
 */
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) {
        modal.classList.add('open');
    }
}

/**
 * Handle creating a new model
 */
async function handleCreateModel() {
    const modelNameInput = document.getElementById('new-model-name');
    const modelTypeSelect = document.getElementById('new-model-type');
    const modelSizeInput = document.getElementById('new-model-size');

    if (!modelNameInput || !modelTypeSelect || !modelSizeInput) return;

    const name = modelNameInput.value.trim();
    const type = modelTypeSelect.value;
    const size = modelSizeInput.value.trim();

    if (!name || !size) {
        showToast('Please fill in all required fields', 'error');
        return;
    }

    try {
        const response = await fetch('/api/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, type, size })
        });

        const result = await response.json();

        if (result.success) {
            showToast('Model created successfully!', 'success');
            closeModal('new-model-overlay');
            modelNameInput.value = '';
            modelSizeInput.value = '';
            await fetchSystemState(true);
        } else {
            showToast(result.message || 'Failed to create model', 'error');
        }
    } catch (error) {
        showToast('Error creating model: ' + error.message, 'error');
    }
}

/**
 * Handle restarting the service
 */
async function handleRestartService() {
    // Show loader overlay
    showRestartLoader();
    
    try {
        const response = await fetch('/api/restart', { method: 'POST' });
        const result = await response.json();

        if (result.success) {
            showToast('Service restarting...', 'success');
            closeModal('restart-confirm-overlay');
            
            // Poll until server is ready
            const ready = await waitForServerToBeReady();
            
            if (ready) {
                // Server is fully ready - show success state briefly before reload
                showToast('Service restarted successfully!', 'success');
                // Keep loader visible during reload
                setTimeout(() => window.location.reload(), 1500);
            } else {
                // Timeout occurred - server might still be starting
                console.log('Health check timeout, attempting reload anyway...');
                // Keep loader visible during reload
                setTimeout(() => window.location.reload(), 2000);
            }
        } else {
            hideRestartLoader();
            showToast(result.message || 'Failed to restart service', 'error');
        }
    } catch (error) {
        hideRestartLoader();
        showToast('Error restarting service: ' + error.message, 'error');
    }
}

/**
 * Show loader overlay during restart
 */
function showRestartLoader() {
    // Remove existing loader if any
    const existingLoader = document.getElementById('restart-loader-overlay');
    if (existingLoader) existingLoader.remove();
    
    // Create loader overlay
    const loaderOverlay = document.createElement('div');
    loaderOverlay.id = 'restart-loader-overlay';
    loaderOverlay.className = 'modal-overlay';
    loaderOverlay.innerHTML = `
        <div class="config-modal" style="max-width: 440px;">
            <div class="config-modal-header">
                <div class="config-modal-title">
                    <i class="fa-solid fa-arrows-spin" style="color: var(--neon-glow); animation: spin 2s linear infinite;"></i>
                    RESTARTING SERVICE
                </div>
            </div>
            <div class="config-modal-body">
                <p style="font-size: 0.9rem; color: var(--text-secondary); line-height: 1.6;">
                    Stopping all models and restarting the system service...
                </p>
                <div style="margin: 20px 0;">
                    <div class="progress-bar" style="width: 100%; height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden;">
                        <div class="progress-fill" style="width: 100%; height: 100%; background: var(--neon-glow); animation: pulse 1.5s ease-in-out infinite;"></div>
                    </div>
                </div>
                <p style="font-size: 0.8rem; color: var(--text-muted); text-align: center;">
                    Please wait while the service initializes...
                </p>
            </div>
        </div>
    `;
    
    document.body.appendChild(loaderOverlay);
}

/**
 * Hide loader overlay after restart completes
 */
function hideRestartLoader() {
    const loader = document.getElementById('restart-loader-overlay');
    if (loader) {
        loader.remove();
    }
}

/**
 * Poll the server until it's back online, with a timeout
 */
async function waitForServerToBeReady(maxWaitMs = 60000, pollIntervalMs = 1000) {
    // Initial 5 second sleep to allow server to start shutting down
    console.log('Waiting 5 seconds for server to begin restart...');
    await new Promise(resolve => setTimeout(resolve, 5000));
    
    const startTime = Date.now();
    
    while (Date.now() - startTime < maxWaitMs) {
        try {
            const response = await fetch('/api/health', { method: 'GET', cache: 'no-store' });
            if (response.ok) {
                const data = await response.json();
                if (data.status === 'ok') {
                    console.log('Server is ready!');
                    return true;
                }
            } else if (response.status === 503) {
                // Server still starting up, continue polling
                console.log('Server is starting up...');
            }
        } catch (e) {
            // Server not ready yet, continue polling
        }
        
        // Wait before next poll
        await new Promise(resolve => setTimeout(resolve, pollIntervalMs));
    }
    
    console.warn('Server still not ready after timeout, proceeding anyway...');
    return false;
}

/**
 * Handle starting all models
 */
async function handleStartAll() {
    if (!confirm('Start all stopped models?')) return;

    try {
        const response = await fetch('/api/start_all', { method: 'POST' });
        const result = await response.json();

        if (result.success) {
            showToast('All models starting...', 'success');
            await fetchSystemState(true);
        } else {
            showToast(result.message || 'Failed to start all models', 'error');
        }
    } catch (error) {
        showToast('Error starting models: ' + error.message, 'error');
    }
}

/**
 * Handle stopping all models
 */
async function handleStopAll() {
    if (!confirm('Stop all running models?')) return;

    try {
        const response = await fetch('/api/stop_all', { method: 'POST' });
        const result = await response.json();

        if (result.success) {
            showToast('All models stopping...', 'success');
            await fetchSystemState(true);
        } else {
            showToast(result.message || 'Failed to stop all models', 'error');
        }
    } catch (error) {
        showToast('Error stopping models: ' + error.message, 'error');
    }
}

/**
 * Open the model config modal
 */
async function openModelConfigModal() {
    // This function is triggered if a config button exists
    // The model-specific config is opened via model cards
    showToast('Use the config button on individual model cards', 'info');
}

/**
 * Open a model's config overlay
 */
async function openModelEditConfig(modelId) {
    const model = appState.models.find(m => m.id === modelId);
    if (!model) return;

    // Populate the config modal with model data
    document.getElementById('config-model-id').value = model.id;
    document.getElementById('config-name').value = model.name || '';
    document.getElementById('config-desc').value = model.desc || '';
    document.getElementById('config-ctxSize').value = model.ctxSize || 16384;
    document.getElementById('config-nParallel').value = model.nParallel || 1;
    document.getElementById('config-batchSize').value = model.batchSize || 2048;
    document.getElementById('config-nGpuLayers').value = model.nGpuLayers || 99;
    document.getElementById('config-threads').value = model.threads || 0;
    document.getElementById('config-port').value = model.port || 9000;
    document.getElementById('config-temperature').value = model.temperature || 0.7;
    document.getElementById('config-maxTokens').value = model.maxTokens || 4096;
    document.getElementById('config-topP').value = model.topP || 0.9;
    document.getElementById('config-extraArgs').value = (model.extraArgs || []).join(' ');

    // Set device selections
    const deviceSelect = document.getElementById('config-device');
    if (deviceSelect && model.devices) {
        const options = deviceSelect.options;
        for (let i = 0; i < options.length; i++) {
            const optVal = options[i].value;
            options[i].selected = model.devices.includes(optVal);
        }
    }

    openModal('model-config-overlay');
}

/**
 * Handle saving model config
 */
async function handleSaveConfig() {
    const modelId = document.getElementById('config-model-id').value;
    if (!modelId) {
        showToast('No model ID found', 'error');
        return;
    }

    const configData = {
        id: modelId,
        name: document.getElementById('config-name').value,
        desc: document.getElementById('config-desc').value,
        ctxSize: parseInt(document.getElementById('config-ctxSize').value) || 16384,
        nParallel: parseInt(document.getElementById('config-nParallel').value) || 1,
        batchSize: parseInt(document.getElementById('config-batchSize').value) || 2048,
        nGpuLayers: parseInt(document.getElementById('config-nGpuLayers').value) || 99,
        threads: parseInt(document.getElementById('config-threads').value) || 0,
        port: parseInt(document.getElementById('config-port').value) || 9000,
        temperature: parseFloat(document.getElementById('config-temperature').value) || 0.7,
        maxTokens: parseInt(document.getElementById('config-maxTokens').value) || 4096,
        topP: parseFloat(document.getElementById('config-topP').value) || 0.9,
        extraArgs: document.getElementById('config-extraArgs').value.split(' ').filter(Boolean)
    };

    // Get selected devices
    const deviceSelect = document.getElementById('config-device');
    if (deviceSelect) {
        configData.devices = Array.from(deviceSelect.selectedOptions).map(o => o.value);
    }

    try {
        const response = await fetch(`/api/models/${modelId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(configData)
        });

        const result = await response.json();

        if (result.success) {
            showToast('Model configuration saved!', 'success');
            closeModal('model-config-overlay');
            await fetchSystemState(true);
        } else {
            showToast(result.message || 'Failed to save configuration', 'error');
        }
    } catch (error) {
        showToast('Error saving config: ' + error.message, 'error');
    }
}

/**
 * Handle resetting config to defaults
 */
async function handleResetConfig() {
    const modelId = document.getElementById('config-model-id').value;
    if (!modelId) return;

    const model = appState.models.find(m => m.id === modelId);
    if (!model) return;

    // Reset to defaults
    document.getElementById('config-name').value = model.name || '';
    document.getElementById('config-desc').value = model.desc || '';
    document.getElementById('config-ctxSize').value = model.ctxSize || 16384;
    document.getElementById('config-nParallel').value = model.nParallel || 1;
    document.getElementById('config-batchSize').value = model.batchSize || 2048;
    document.getElementById('config-nGpuLayers').value = model.nGpuLayers || 99;
    document.getElementById('config-threads').value = model.threads || 0;
    document.getElementById('config-port').value = model.port || 9000;
    document.getElementById('config-temperature').value = model.temperature || 0.7;
    document.getElementById('config-maxTokens').value = model.maxTokens || 4096;
    document.getElementById('config-topP').value = model.topP || 0.9;
    document.getElementById('config-extraArgs').value = (model.extraArgs || []).join(' ');

    const deviceSelect = document.getElementById('config-device');
    if (deviceSelect && model.devices) {
        const options = deviceSelect.options;
        for (let i = 0; i < options.length; i++) {
            options[i].selected = model.devices.includes(options[i].value);
        }
    }

    showToast('Config reset to model defaults', 'success');
}

/**
 * Show toast notification
 */
function showToast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = `toast toast-${type} animate-progress`;
    toast.innerHTML = `
        <i class="fa-solid ${type === 'success' ? 'fa-check-circle' : type === 'error' ? 'fa-exclamation-circle' : 'fa-info-circle'}"></i>
        <span class="toast-message">${message}</span>
        <button class="toast-close" aria-label="Close notification">
            <i class="fa-solid fa-xmark"></i>
        </button>
    `;
    container.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => {
        toast.classList.add('show');
    });

    // Auto-remove after 4 seconds
    const removeTimeout = setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 4000);

    // Allow manual close before timeout
    toast.querySelector('.toast-close').addEventListener('click', (e) => {
        e.stopPropagation();
        clearTimeout(removeTimeout);
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    });

    // Keep toast visible on hover (better UX)
    toast.addEventListener('mouseenter', () => {
        const progress = toast.querySelector('::after');
        if (progress) {
            toast.style.setProperty('--animation-paused', 'true');
            toast.classList.remove('animate-progress');
            toast.classList.add('pause-progress');
        }
    });

    toast.addEventListener('mouseleave', () => {
        toast.classList.remove('pause-progress');
        toast.classList.add('animate-progress');
    });
}

function updateClock() {
    const clockNode = document.getElementById('system-time');
    const now = new Date();
    clockNode.textContent = now.toTimeString().split(' ')[0];
}

// REST API calls
async function fetchSystemState(forceRender = false) {
    try {
        // Fetch model status and host stats
        const statusRes = await fetch('/api/status');
        if (!statusRes.ok) throw new Error('API server returned error');
        const statusData = await statusRes.json();

        appState.models = statusData.models;
        appState.host = statusData.host;

        // Fetch GPU stats
        const gpuRes = await fetch('/api/gpu');
        if (gpuRes.ok) {
            const gpuData = await gpuRes.json();
            appState.gpus = gpuData;
        }

        // Fetch server mode (single_port vs multi_port)
        try {
            const configRes = await fetch('/api/config');
            if (configRes.ok) {
                const configData = await configRes.json();
                appState.serverMode = configData.mode || 'single_port';
            }
        } catch (e) {
            // Config fetch failed, keep existing mode
        }

        renderDashboard(forceRender);
        updateChatModelDropdown();
        appState.isInitialized = true;
    } catch (err) {
        console.error('Failed to sync system status:', err);
    }
}

function renderDashboard(forceRender = false) {
    // 1. CPU Tile (telemetry grid)
    updateCpuTile(appState.host);

    // 1b. RAM Tile (telemetry grid)
    updateRamTile(appState.host);

    // 2. Host Stats (guarded: elements may not exist in all layouts)
    const _hostCpu = document.getElementById('host-cpu');
    if (_hostCpu) _hostCpu.textContent = appState.host.cpu_load ? appState.host.cpu_load.toFixed(2) : '0.00';
    const _hostRam = document.getElementById('host-ram');
    if (_hostRam) _hostRam.textContent = appState.host.mem_pct ? appState.host.mem_pct + '%' : '0%';
    const _hostRamBar = document.getElementById('host-ram-bar');
    if (_hostRamBar) _hostRamBar.style.width = appState.host.mem_pct ? appState.host.mem_pct + '%' : '0%';

    // 3. GPU Telemetry — dynamic tiles
    renderGpuTiles(appState.gpus);

    // 3. Unified Grid List (build once if forced or not initialized, then update nodes)
    const grid = document.getElementById('models-grid');

    if (forceRender || !appState.isInitialized || grid.children.length === 0) {
        grid.innerHTML = '';
        appState.models.forEach(model => {
            const card = createModelCardDOM(model);
            grid.appendChild(card);
        });
    } else {
        // Update nodes selectively to prevent flickering
        appState.models.forEach(model => {
            updateModelCardDOM(model);
        });
    }
}

function updateCpuTile(host) {
    if (!host || Object.keys(host).length === 0) return;

    // CPU load gauge (displayed as average load mapped to 0-100% scale, capped at cpu_count)
    const cpuValue = document.getElementById('cpu-value');
    const cpuLoadavg = document.getElementById('cpu-loadavg');
    const cpuCores = document.getElementById('cpu-cores');
    const cpuTemp = document.getElementById('cpu-temp');
    const cpuGaugeProgress = document.getElementById('cpu-gauge-progress');
    const cpuModel = document.getElementById('cpu-model');

    if (cpuValue) {
        const load = host.cpu_load || 0;
        const cpuCount = host.cpu_count || 1;
        // Load average mapped to percentage of total capacity
        const loadPct = Math.min(100, (load / cpuCount) * 100);
        cpuValue.textContent = loadPct.toFixed(0);

        // Update gauge ring rotation (0-270deg mapped from 0-100%)
        if (cpuGaugeProgress) {
            const rotation = (loadPct / 100) * 270;
            cpuGaugeProgress.style.transform = `rotate(${rotation}deg)`;
        }
    }

    if (cpuLoadavg) {
        const avg1 = host.load_avg_1m || 0;
        const avg5 = host.load_avg_5m || 0;
        const avg15 = host.load_avg_15m || 0;
        cpuLoadavg.textContent = `${avg1.toFixed(2)}, ${avg5.toFixed(2)}, ${avg15.toFixed(2)}`;
    }

    if (cpuCores) {
        cpuCores.textContent = host.cpu_count || '--';
    }

    if (cpuTemp) {
        const temp = host.cpu_temp;
        if (temp !== undefined && temp !== null) {
            cpuTemp.textContent = `${temp.toFixed(0)}°C`;
        } else {
            cpuTemp.textContent = '--°C';
        }
    }

    // Update CPU model name from host
    if (cpuModel && host.cpu_model) {
        cpuModel.textContent = host.cpu_model;
    }
}

function updateRamTile(host) {
    if (!host || Object.keys(host).length === 0) return;

    const ramValue = document.getElementById('ram-value');
    const ramGaugeProgress = document.getElementById('ram-gauge-progress');
    const ramBar = document.getElementById('ram-bar');
    const ramUsed = document.getElementById('ram-used');
    const ramCached = document.getElementById('ram-cached');
    const ramSwap = document.getElementById('ram-swap');

    const memPct = host.mem_pct || 0;
    const memUsed = host.mem_used || 0;  // MiB
    const memTotal = host.mem_total || 0;  // MiB
    const memCached = host.mem_cached || 0;  // MiB
    const swapUsed = host.swap_used || 0;  // MiB

    // Gauge value (percentage)
    if (ramValue) ramValue.textContent = memPct.toFixed(1);

    // Gauge ring rotation (0-270deg mapped from 0-100%)
    if (ramGaugeProgress) {
        const rotation = (memPct / 100) * 270;
        ramGaugeProgress.style.transform = `rotate(${rotation}deg)`;
    }

    // Used bar
    if (ramBar) ramBar.style.width = `${memPct}%`;

    // Used text: "X.X / Y.Y GB"
    if (ramUsed) {
        ramUsed.textContent = `${(memUsed / 1024).toFixed(1)} / ${(memTotal / 1024).toFixed(1)} GB`;
    }

    // Cached text
    if (ramCached) {
        ramCached.textContent = `${(memCached / 1024).toFixed(1)} GB`;
    }

    // Swap text
    if (ramSwap) {
        ramSwap.textContent = swapUsed > 0 ? `${(swapUsed / 1024).toFixed(1)} GB` : '0.0 GB';
    }
}

/**
 * Render GPU tiles dynamically based on what /api/gpu returns.
 * Server returns { nvidia: data|null, amd: data|null } where each has:
 *   { name, temp, util, mem_used, mem_total, power }
 * Only renders tiles for detected GPUs (up to 3 for consumers).
 */
let _gpuTileKeys = []; // track which gpu tiles exist for incremental update

function renderGpuTiles(gpus) {
    if (!gpus) return;

    const grid = document.getElementById('telemetry-grid');
    if (!grid) return;

    // Build list of detected GPUs: { key, brand, data }
    const detected = [];
    if (gpus.nvidia) detected.push({ key: 'nvidia', brand: 'nvidia', data: gpus.nvidia });
    if (gpus.amd) detected.push({ key: 'amd', brand: 'amd', data: gpus.amd });

    // Cap at 3 GPUs for consumer setups
    const gpusToShow = detected.slice(0, 3);
    const currentKeys = gpusToShow.map(g => g.key);

    // Remove tiles for GPUs no longer detected
    const existingTileIds = Array.from(grid.querySelectorAll('.gpu-tile')).map(el => el.id);
    for (const tileId of existingTileIds) {
        const tileKey = tileId.replace('tile-gpu-', '');
        if (!_gpuTileKeys.includes(tileKey) || !currentKeys.includes(tileKey)) {
            if (!_gpuTileKeys.includes(tileKey)) {
                // never tracked, shouldn't happen
            } else if (!currentKeys.includes(tileKey)) {
                document.getElementById(tileId)?.remove();
            }
        }
    }

    // Build or update tiles
    for (const gpu of gpusToShow) {
        let tile = document.getElementById(`tile-gpu-${gpu.key}`);
        if (!tile) {
            tile = document.createElement('div');
            tile.id = `tile-gpu-${gpu.key}`;
            tile.className = `telemetry-tile ${gpu.brand}-tile gpu-tile`;
            _gpuTileKeys.push(gpu.key);
            grid.appendChild(tile);
        }
        updateSingleGpuTile(tile, gpu);
    }

    _gpuTileKeys = currentKeys;
}

function updateSingleGpuTile(tile, gpu) {
    const { brand, data } = gpu;
    const isNvidia = brand === 'nvidia';

    // Find running model on this GPU
    const runningModel = appState.models.find(m => {
        if (m.status !== 'running') return false;
        const gpuUpper = (m.gpu || '').toUpperCase();
        if (isNvidia) return gpuUpper.includes('NVIDIA') || gpuUpper.includes('CUDA');
        return gpuUpper.includes('AMD') || gpuUpper.includes('ROCm');
    });

    const gpuName = data.name || (isNvidia ? 'NVIDIA GPU' : 'AMD GPU');
    // Trim brand prefixes for cleaner display
    const cleanName = gpuName.replace(/^NVIDIA\s*/i, '').replace(/^GeForce\s*/i, '').replace(/^AMD\s*/i, '').replace(/^Radeon\s*/i, '').trim();
    const shortName = cleanName.length > 20 ? cleanName.substring(0, 20) + '…' : cleanName;
    const util = data.util || 0;
    const temp = data.temp || 0;
    const tempPct = Math.min(100, (temp / 90) * 100);
    const memUsed = data.mem_used || 0;
    const memTotal = data.mem_total || 0;
    const vramPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
    const power = data.power || 0;

    const neonVar = isNvidia ? 'var(--nvidia-neon)' : 'var(--amd-neon)';
    const glowVar = isNvidia ? 'var(--nvidia-glow)' : 'var(--amd-glow)';
    const brandLabel = isNvidia ? 'NVIDIA' : 'AMD';

    const statusColor = runningModel ? neonVar : 'var(--text-muted)';
    const statusText = runningModel ? runningModel.name.toUpperCase() : 'STANDBY';

    tile.className = `telemetry-tile ${brand}-tile gpu-tile`;
    tile.innerHTML = `
        <div class="tile-header">
            <div class="tile-label">
                <div class="tile-icon"><i class="fa-solid fa-microchip" style="${isNvidia ? 'color: var(--nvidia-neon)' : ''}"></i></div>
                <span class="tile-title">${brandLabel}</span>
            </div>
            <span class="tile-badge" style="color: ${statusColor}; border-color: ${runningModel ? (isNvidia ? 'rgba(118,185,0,0.3)' : 'rgba(0,243,255,0.3)') : 'var(--glass-border)'};">${statusText}</span>
        </div>
        <div class="tile-body">
            <div class="circular-gauge">
                <div class="gauge-ring"></div>
                <div class="gauge-ring-progress" style="transform: rotate(${(util / 100) * 270}deg);"></div>
                <div class="gauge-center">
                    <span class="gauge-value">${util.toFixed(0)}</span>
                    <span class="gauge-unit">UTIL</span>
                    <span class="gauge-subtitle">${shortName}</span>
                </div>
            </div>
            <div class="tile-details">
                <div class="detail-bar-row">
                    <span class="detail-label">TEMP</span>
                    <div class="detail-bar-bg">
                        <div class="detail-bar-fill temp-bar-fill" style="width: ${tempPct}%"></div>
                    </div>
                    <span class="detail-bar-value">${temp > 0 ? temp.toFixed(0) + '°C' : '--°C'}</span>
                </div>
                <div class="detail-bar-row">
                    <span class="detail-label">VRAM</span>
                    <div class="detail-bar-bg">
                        <div class="detail-bar-fill vram-bar-fill" style="width: ${vramPct}%"></div>
                    </div>
                    <span class="detail-bar-value">${(memUsed / 1024).toFixed(1)} / ${(memTotal / 1024).toFixed(1)} GB</span>
                </div>
                <div class="detail-bar-row">
                    <span class="detail-label">POWER</span>
                    <span class="detail-bar-value">${power > 0 ? power.toFixed(1) + ' W' : '-- W'}</span>
                </div>
            </div>
        </div>
    `;
}

function updateGpuCard(prefix, gpuData) {
    const activeText = document.getElementById(`${prefix}-active`);
    const tempText = document.getElementById(`${prefix}-temp`);
    const tempFill = document.getElementById(`${prefix}-temp-bar`);
    const loadText = document.getElementById(`${prefix}-load`);
    const loadFill = document.getElementById(`${prefix}-load-fill`);
    const vramText = document.getElementById(`${prefix}-vram-text`);
    const vramBar = document.getElementById(`${prefix}-vram-bar`);
    const powerText = document.getElementById(`${prefix}-power`);

    // Find running model on this GPU (case-insensitive matching)
    const runningModel = appState.models.find(m => {
        if (m.status !== 'running') return false;
        const gpuUpper = (m.gpu || '').toUpperCase();
        if (prefix === 'nvidia') return gpuUpper.includes('NVIDIA') || gpuUpper.includes('CUDA') || gpuUpper.includes('GPU');
        if (prefix === 'amd') return gpuUpper.includes('AMD') || gpuUpper.includes('ROCm') || gpuUpper.includes('GPU');
        return false;
    });
    if (activeText) {
        if (runningModel) {
            activeText.textContent = runningModel.name.toUpperCase();
            activeText.style.color = prefix === 'nvidia' ? 'var(--nvidia-neon)' : 'var(--amd-neon)';
            activeText.style.borderColor = prefix === 'nvidia' ? 'rgba(118, 185, 0, 0.3)' : 'rgba(0, 243, 255, 0.3)';
        } else {
            activeText.textContent = 'INACTIVE';
            activeText.style.color = 'var(--text-muted)';
            activeText.style.borderColor = 'var(--glass-border)';
        }
    }

    if (gpuData) {
        // Temp (max expected 100C)
        const temp = gpuData.temp || 0;
        if (tempText) tempText.textContent = temp.toFixed(0);
        const tempPct = Math.min(100, (temp / 90) * 100); // 90C as full scale
        if (tempFill) tempFill.style.strokeDashoffset = GAUGE_CIRCUMFERENCE - (GAUGE_CIRCUMFERENCE * tempPct) / 100;

        // Load (util)
        const load = gpuData.util || 0;
        if (loadText) loadText.textContent = load.toFixed(0);
        if (loadFill) loadFill.style.strokeDashoffset = GAUGE_CIRCUMFERENCE - (GAUGE_CIRCUMFERENCE * load) / 100;

        // VRAM
        const used = gpuData.mem_used || 0;
        const total = gpuData.mem_total || 0;
        if (vramText) vramText.textContent = `${used.toLocaleString()} / ${total.toLocaleString()} MiB`;
        const vramPct = total > 0 ? (used / total) * 100 : 0;
        if (vramBar) vramBar.style.width = `${vramPct}%`;

        // Update VRAM text (use text element if exists, otherwise use bar element)
        const vramTextEl = document.getElementById(`${prefix}-vram`) || vramText;
        if (vramTextEl) {
            vramTextEl.textContent = `${(used/1024).toFixed(1)} / ${(total/1024).toFixed(1)} GB`;
        }

        // Power
        if (powerText) powerText.textContent = gpuData.power ? `${gpuData.power.toFixed(1)} W` : 'N/A';
    } else {
        if (tempText) tempText.textContent = '--°C';
        if (tempFill) tempFill.style.width = '0%';
        if (loadText) loadText.textContent = '--';
        if (loadFill) loadFill.style.strokeDashoffset = GAUGE_CIRCUMFERENCE;
        const vramTextEl = document.getElementById(`${prefix}-vram`);
        if (vramTextEl) vramTextEl.textContent = '0.0 / 0.0 GB';
        if (vramBar) vramBar.style.width = '0%';
        if (powerText) powerText.textContent = '-- W';
    }
}

function createModelCardDOM(model) {
    const card = document.createElement('div');
    card.id = `card-${model.id}`;

    // Determine card layout and classes (case-insensitive)
    const isDualGpu = (model.gpu || '').toLowerCase().includes('dual') || (model.gpu || '').toLowerCase().includes('nvidia');
    card.className = `model-card ${isDualGpu ? 'dual-group-card' : 'amd-group-card'}`;

    // Construct display command
    const binary = "./build/bin/llama-server";
    const deviceStr = isDualGpu ? "ROCm0,Vulkan2" : "ROCm0";
    const modelArg = `~/models/${model.filename}`;
    const mmprojArg = model.mmproj ? ` \\\n  --mmproj ~/models/${model.mmproj}` : '';
    const extraArgs = isDualGpu ? "--ctx-size 8192 -np 1" : "-ngl 99 --ctx-size 65536";
    let extraArgsStr = extraArgs;
    if (model.extraArgs && model.extraArgs.length > 0) {
        extraArgsStr += ` \\\n  ${model.extraArgs.join(' ')}`;
    }
    const displayCmd = `${binary} \\\n  --model ${modelArg}${mmprojArg} \\\n  --device ${deviceStr} \\\n  ${extraArgsStr} \\\n  --port ${model.port}`;

    card.innerHTML = `
        <div class="model-card-header">
            <div class="model-name-wrapper">
                <span class="model-name">${escapeHtml(model.name)}</span>
                <span class="model-endpoint-badge" onclick="copyEndpoint(event, '${escapeHtml(model.endpoint)}')" title="Click to copy endpoint">
                    <i class="fa-solid fa-link"></i> ${escapeHtml(model.endpoint)}
                </span>
            </div>
            <span class="model-status-pill status-stopped" id="status-${model.id}">
                <i class="fa-solid fa-circle"></i> STOPPED
            </span>
        </div>
        
        <div class="model-meta-row">
            <span class="meta-badge size-badge" title="Model VRAM Footprint"><i class="fa-solid fa-weight-hanging"></i> ${escapeHtml(model.size)}</span>
            <span class="meta-badge hw-badge ${isDualGpu ? 'dual-hw' : 'amd-hw'}" title="Hardware Engine">
                <i class="${isDualGpu ? 'fa-solid fa-layer-group' : 'fa-solid fa-microchip'}"></i> 
                ${isDualGpu ? 'Dual GPU (ROCm+Vulkan)' : 'AMD R9700 (ROCm)'}
            </span>
        </div>
        
        <div class="model-desc">${escapeHtml(model.desc)}</div>

        <div class="model-command-preview" id="cmd-${model.id}" title="Execution command">
            ${escapeHtml(displayCmd)}
        </div>

        <div class="model-process-stats" id="stats-${model.id}" style="display: none;">
            <div class="stat-box">
                <span class="stat-label">PID</span>
                <span class="stat-value" id="pid-${model.id}">--</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">CPU</span>
                <span class="stat-value" id="cpu-${model.id}">--</span>
            </div>
            <div class="stat-box">
                <span class="stat-label">Memory</span>
                <span class="stat-value" id="mem-${model.id}">--</span>
            </div>
        </div>

        <div class="gpu-details-row" id="uptime-row-${model.id}" style="display: none;">
            <span>PROCESS UPTIME</span>
            <span id="uptime-${model.id}" class="telemetry-highlight">00:00</span>
        </div>

        <div class="model-card-actions">
            <button class="btn btn-secondary btn-config" onclick="openModelEditConfig('${escapeHtml(model.id)}')" title="Edit model configuration">
                <i class="fa-solid fa-gear"></i> CONFIG
            </button>
            <button class="btn btn-secondary btn-logs" onclick="openConsole('${escapeHtml(model.id)}')">
                <i class="fa-solid fa-terminal"></i> LOGS
            </button>
            <button class="btn btn-start" id="btn-toggle-${model.id}" onclick="toggleModel('${escapeHtml(model.id)}')">
                <i class="fa-solid fa-play"></i> START SERVER
            </button>
        </div>
    `;

    return card;
}

function updateModelCardDOM(model) {
    const card = document.getElementById(`card-${model.id}`);
    if (!card) return;

    const statusPill = document.getElementById(`status-${model.id}`);
    const statsBox = document.getElementById(`stats-${model.id}`);
    const uptimeRow = document.getElementById(`uptime-row-${model.id}`);
    const cmdPreview = document.getElementById(`cmd-${model.id}`);

    const pidVal = document.getElementById(`pid-${model.id}`);
    const cpuVal = document.getElementById(`cpu-${model.id}`);
    const memVal = document.getElementById(`mem-${model.id}`);
    const uptimeVal = document.getElementById(`uptime-${model.id}`);
    const toggleBtn = document.getElementById(`btn-toggle-${model.id}`);

    // Single port mode logic: only one model can run at a time
    const isSinglePort = appState.serverMode === 'single_port';
    const anyRunning = appState.models.some(m => m.status === 'running');
    const isRunning = model.status === 'running';

    if (isRunning) {
        // Model is running (works for both single_port and multi_port modes)
        card.classList.add('running-card');
        card.classList.remove('switching-card');

        statusPill.className = 'model-status-pill status-running';
        statusPill.innerHTML = '<i class="fa-solid fa-circle-play"></i> RUNNING';

        statsBox.style.display = 'grid';
        uptimeRow.style.display = 'flex';
        cmdPreview.style.display = 'none';

        pidVal.textContent = model.pid;
        cpuVal.textContent = `${model.cpu}%`;
        memVal.textContent = `${model.memory}%`;
        uptimeVal.textContent = model.uptime;

        toggleBtn.className = 'btn btn-stop';
        toggleBtn.innerHTML = '<i class="fa-solid fa-stop"></i> STOP SERVER';
        toggleBtn.disabled = false;
    } else {
        // Normal stopped state (works for both single-port and multi-port)
        // In single-port mode, STOPPED models remain clickable to allow switching
        card.classList.remove('running-card');
        card.classList.remove('switching-card');

        statusPill.className = 'model-status-pill status-stopped';
        statusPill.innerHTML = '<i class="fa-solid fa-circle"></i> STOPPED';

        statsBox.style.display = 'none';
        uptimeRow.style.display = 'none';
        cmdPreview.style.display = 'block';

        toggleBtn.className = 'btn btn-start';
        toggleBtn.innerHTML = '<i class="fa-solid fa-play"></i> START SERVER';
        toggleBtn.disabled = false;
        toggleBtn.style.opacity = '1';
        toggleBtn.style.cursor = 'pointer';
    }
}

// Copy utilities
function copyEndpoint(e, endpoint) {
    const badge = e.target.closest('.model-endpoint-badge');
    navigator.clipboard.writeText(endpoint).then(() => {
        // Simple micro glow-toast
        const oldHtml = badge.innerHTML;
        badge.innerHTML = '<i class="fa-solid fa-check" style="color: var(--nvidia-neon);"></i> COPIED!';
        setTimeout(() => {
            badge.innerHTML = oldHtml;
        }, 1200);
    });
}

// Controller functions
async function toggleModel(modelId) {
    const model = appState.models.find(m => m.id === modelId);
    if (!model) return;

    // Single port mode: check if another model is running and show confirmation modal
    const isSinglePort = appState.serverMode === 'single_port';
    const anyRunning = appState.models.some(m => m.status === 'running');

    if (model.status === 'stopped' && isSinglePort && anyRunning) {
        const runningModel = appState.models.find(m => m.status === 'running');
        const confirmMessage = document.getElementById('single-port-confirm-message');
        if (confirmMessage) {
            confirmMessage.innerHTML = `Starting <strong>${escapeHtml(model.name)}</strong> will stop the currently running model <strong>${escapeHtml(runningModel.name)}</strong>. Continue?`;
        }
        pendingModelId = modelId;
        openModal('single-port-confirm-overlay');
        return;
    }

    // Direct toggle (no confirmation needed)
    executeModelToggle(modelId);
}

/**
 * Execute the actual model toggle (start/stop) after confirmation or when no confirmation needed
 */
async function executeModelToggle(modelId) {
    const model = appState.models.find(m => m.id === modelId);
    if (!model) return;

    const toggleBtn = document.getElementById(`btn-toggle-${modelId}`);
    const statusPill = document.getElementById(`status-${modelId}`);

    if (model.status === 'stopped') {
        // Start process
        toggleBtn.disabled = true;
        statusPill.className = 'model-status-pill status-loading';
        statusPill.innerHTML = '<i class="fa-solid fa-spinner"></i> DEPLOYING...';

        try {
            const res = await fetch('/api/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: modelId })
            });

            if (!res.ok) {
                throw new Error(`Failed to start: ${res.statusText}`);
            }

            await res.json();

            // Open console on successful start
            openConsole(modelId);
        } catch (err) {
            toggleBtn.disabled = false;
            statusPill.className = 'model-status-pill';
            statusPill.innerHTML = '<i class="fa-solid fa-circle-xmark"></i> FAILED';
            console.error('Start failed:', err);
            return;
        }
    } else if (model.status === 'running') {
        // Stop process
        toggleBtn.disabled = true;
        statusPill.className = 'model-status-pill status-loading';
        statusPill.innerHTML = '<i class="fa-solid fa-spinner"></i> STOPPING...';

        try {
            const res = await fetch('/api/stop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: modelId })
            });

            if (!res.ok) {
                throw new Error(`Failed to stop: ${res.statusText}`);
            }

            await res.json();
            model.status = 'stopped';
            statusPill.className = 'model-status-pill';
            statusPill.innerHTML = '<i class="fa-solid fa-circle-xmark"></i> STOPPED';
        } catch (err) {
            toggleBtn.disabled = false;
            statusPill.className = 'model-status-pill';
            statusPill.innerHTML = '<i class="fa-solid fa-circle-xmark"></i> FAILED';
            console.error('Stop failed:', err);
            return;
        }
    }

    toggleBtn.disabled = false;
}

/**
 * Open the console/drawer for a model's logs
 */
function openConsole(modelId) {
    appState.activeLogModel = modelId;
    const model = appState.models.find(m => m.id === modelId);

    const modelLabel = document.getElementById('console-model-label');
    const drawer = document.getElementById('logs-drawer');
    const content = document.getElementById('console-content');

    if (modelLabel) {
        modelLabel.textContent = model ? model.name.toUpperCase() : modelId.toUpperCase();
    }

    // Show drawer
    if (drawer) {
        drawer.classList.add('open');
    }
    if (content) {
        content.textContent = 'Opening console socket connection...';
    }

    // Poll logs immediately
    pollLogs();

    // Start continuous log polling (every 1.5 seconds)
    appState.logPollIntervalId = setInterval(pollLogs, 1500);
}

function closeConsole() {
    const drawer = document.getElementById('logs-drawer');
    drawer.classList.remove('open');

    if (appState.logPollIntervalId) {
        clearInterval(appState.logPollIntervalId);
        appState.logPollIntervalId = null;
    }
    appState.activeLogModel = null;
}

async function pollLogs() {
    if (!appState.activeLogModel) return;

    try {
        const res = await fetch(`/api/logs?model=${escapeHtml(appState.activeLogModel)}&lines=150`);
        if (!res.ok) throw new Error('Log read failure');
        const data = await res.json();

        const content = document.getElementById('console-content');
        const body = document.getElementById('console-body');

        content.textContent = data.logs || 'Server log is empty.';

        // Auto-scroll to bottom on each poll
        body.scrollTop = body.scrollHeight;
    } catch (err) {
        console.error('Logs polling error:', err);
    }
}

// Chat Playground logic
function togglePlayground() {
    const drawer = document.getElementById('playground-drawer');
    drawer.classList.toggle('open');
}

function closePlayground() {
    const drawer = document.getElementById('playground-drawer');
    drawer.classList.remove('open');
}

function updateChatModelDropdown() {
    const select = document.getElementById('chat-model-select');
    const runningModels = appState.models.filter(m => m.status === 'running');

    const previousSelection = select.value;

    // Clear and keep first option
    select.innerHTML = '<option value="">-- Select Running Model --</option>';

    runningModels.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.name;
        select.appendChild(opt);
    });

    // Restore previous selection if still running
    if (runningModels.some(m => m.id === previousSelection)) {
        select.value = previousSelection;
    } else if (select.value === '' && runningModels.length > 0) {
        // Automatically select the first running model to be helpful!
        select.value = runningModels[0].id;
        handleChatModelChange();
    } else if (runningModels.length === 0) {
        select.value = '';
        handleChatModelChange();
    }
}

function handleChatModelChange() {
    const select = document.getElementById('chat-model-select');
    const modelId = select.value;

    const alertBox = document.getElementById('playground-alert');
    const chatInput = document.getElementById('chat-input');
    const sendBtn = document.getElementById('btn-send-chat');

    if (modelId) {
        appState.chatModelId = modelId;
        alertBox.style.display = 'none';
        chatInput.disabled = false;
        sendBtn.disabled = false;

        // Reset chat history when changing models
        appState.chatHistory = [];
        const modelData = appState.models.find(m => m.id === modelId);
        const modelName = modelData?.name || 'LLM';
        const messagesContainer = document.getElementById('chat-messages');
        messagesContainer.innerHTML = `
            <div class="chat-bubble assistant-bubble system-message">
                <div class="bubble-avatar"><i class="fa-solid fa-robot"></i></div>
                <div class="bubble-content">
                    <strong>SAFIYUWS Core Playground</strong>
                    <p>Inference pipeline connected with <strong>${escapeHtml(modelName)}</strong>. Ready for prompts.</p>
                </div>
            </div>
        `;
    } else {
        appState.chatModelId = null;
        alertBox.style.display = 'flex';
        chatInput.disabled = true;
        sendBtn.disabled = true;
        chatInput.value = '';
    }
}

async function handleSendChat() {
    const inputNode = document.getElementById('chat-input');
    const prompt = inputNode.value.trim();
    if (!prompt || !appState.chatModelId) return;

    inputNode.value = '';
    inputNode.disabled = true;
    document.getElementById('btn-send-chat').disabled = true;

    const messagesContainer = document.getElementById('chat-messages');

    // 1. Add User bubble
    appendBubble('user', prompt);
    appState.chatHistory.push({ role: 'user', content: prompt });

    // 2. Add Assistant streaming bubble
    const assistantId = 'stream-' + Date.now();
    appendStreamingBubble(assistantId);

    // Scroll messages
    const body = document.getElementById('playground-body');
    body.scrollTop = body.scrollHeight;

    const selectedModel = appState.models.find(m => m.id === appState.chatModelId);
    if (!selectedModel) {
        replaceStreamingBubbleWithError(assistantId, 'Selected model is no longer active.');
        resetInputState();
        return;
    }

    try {
        // SSE streaming request to llama-server OpenAI endpoint
        const response = await fetch(`${selectedModel.endpoint}/chat/completions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                messages: appState.chatHistory,
                temperature: 0.7,
                max_tokens: 4096,
                stream: true
            })
        });

        if (!response.ok) {
            throw new Error(`Inference fail (Code ${response.status})`);
        }

        // Read SSE stream chunk by chunk
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let fullReply = '';
        const contentEl = document.getElementById(`${assistantId}-content`);

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop(); // keep incomplete line in buffer

            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed || !trimmed.startsWith('data: ')) continue;

                const dataStr = trimmed.slice(6); // remove 'data: ' prefix
                if (dataStr === '[DONE]') {
                    // Stream complete
                    appState.chatHistory.push({ role: 'assistant', content: fullReply });
                    const titleEl = document.getElementById(`${assistantId}-name`);
                    if (titleEl) {
                        titleEl.innerHTML = `<span class="chat-model-badge">${escapeHtml(selectedModel.name)}</span>`;
                    }
                    // Mark bubble as non-streaming
                    contentEl.classList.remove('streaming');
                    continue;
                }

                try {
                    const json = JSON.parse(dataStr);
                    const delta = json.choices?.[0]?.delta?.content;
                    if (delta) {
                        fullReply += delta;
                        if (contentEl) {
                            contentEl.textContent = fullReply;
                            // Auto-scroll
                            body.scrollTop = body.scrollHeight;
                        }
                    }
                } catch (e) {
                    // Skip non-JSON SSE lines
                }
            }
        }

        // Stream complete
        if (contentEl) contentEl.classList.remove('streaming');

    } catch (err) {
        console.error('Inference error:', err);
        replaceStreamingBubbleWithError(assistantId, `Failed to obtain completion.\n\n${escapeHtml(err.message)}`);
    } finally {
        resetInputState();
        body.scrollTop = body.scrollHeight;
    }
}

function appendBubble(role, text) {
    const container = document.getElementById('chat-messages');
    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role}-bubble`;

    const avatarIcon = role === 'user' ? 'fa-user' : 'fa-brain';
    const modelForName = appState.models.find(m => m.id === appState.chatModelId);
    const authorName = role === 'user' ? 'You' : (modelForName?.name || 'LLM');

    bubble.innerHTML = `
        <div class="bubble-avatar"><i class="fa-solid ${avatarIcon}"></i></div>
        <div class="bubble-content">
            <strong>${authorName}</strong>
            <p>${escapeHtml(text)}</p>
        </div>
    `;
    container.appendChild(bubble);
}

function appendThinkingBubble() {
    const container = document.getElementById('chat-messages');
    const bubble = document.createElement('div');
    const id = 'thinking-' + Date.now();
    bubble.id = id;
    bubble.className = 'chat-bubble assistant-bubble';

    const thinkingModel = appState.models.find(m => m.id === appState.chatModelId);
    const authorName = thinkingModel?.name || 'LLM';

    bubble.innerHTML = `
        <div class="bubble-avatar"><i class="fa-solid fa-brain"></i></div>
        <div class="bubble-content">
            <strong>${authorName} (THINKING...)</strong>
            <div class="typing-indicator">
                <span></span>
                <span></span>
                <span></span>
            </div>
        </div>
    `;
    container.appendChild(bubble);
    return id;
}

function removeBubble(id) {
    const node = document.getElementById(id);
    if (node) node.remove();
}

function appendStreamingBubble(assistantId) {
    const container = document.getElementById('chat-messages');
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble assistant-bubble';
    bubble.id = assistantId;

    const selectedModel = appState.models.find(m => m.id === appState.chatModelId);
    const modelName = selectedModel?.name || 'LLM';

    bubble.innerHTML = `
        <div class="bubble-avatar"><i class="fa-solid fa-brain"></i></div>
        <div class="bubble-content">
            <strong id="${assistantId}-name"><span class="chat-model-badge">${escapeHtml(modelName)}</span> <span class="streaming-label">(streaming...)</span></strong>
            <div id="${assistantId}-content" class="streaming"><div class="typing-indicator"><span></span><span></span><span></span></div></div>
        </div>
    `;
    container.appendChild(bubble);
    return assistantId;
}

function replaceStreamingBubbleWithError(assistantId, errorMsg) {
    const bubble = document.getElementById(assistantId);
    if (!bubble) return;
    const contentEl = document.getElementById(`${assistantId}-content`);
    const titleEl = document.getElementById(`${assistantId}-name`);

    if (contentEl) {
        contentEl.classList.remove('streaming');
        contentEl.innerHTML = `<p class="error-text">${escapeHtml(errorMsg)}</p>`;
    }
    if (titleEl) {
        titleEl.innerHTML = `<span class="chat-model-badge">${errorMsg.includes('no longer active') ? 'ERROR' : 'LLM'}</span>`;
    }
}

function resetInputState() {
    const inputNode = document.getElementById('chat-input');
    inputNode.disabled = false;
    inputNode.focus();
    document.getElementById('btn-send-chat').disabled = false;
}

function escapeHtml(text) {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

/**
 * Export all model configurations as JSON file
 */
async function handleExportModels() {
    try {
        const response = await fetch('/api/models/export');
        if (!response.ok) throw new Error('Export failed');
        
        const data = await response.json();
        if (!data.success) {
            showToast(data.message || 'Export failed', 'error');
            return;
        }

        // Create download link for JSON file
        const modelsJson = JSON.stringify(data.models, null, 2);
        const blob = new Blob([modelsJson], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        
        const a = document.createElement('a');
        a.href = url;
        a.download = data.filename || 'llamashift-models-export.json';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        
        URL.revokeObjectURL(url);
        showToast(`Exported ${data.count} model(s) successfully!`, 'success');
    } catch (error) {
        showToast('Export failed: ' + error.message, 'error');
    }
}

/**
 * Handle file selection for import
 */
function handleImportFileSelect(event) {
    const file = event.target.files[0];
    const selectedText = document.getElementById('import-file-selected');
    
    if (file) {
        if (selectedText) {
            selectedText.textContent = `Selected: ${escapeHtml(file.name)}`;
            selectedText.style.color = 'var(--text-primary)';
        }
        
        // Enable the import button
        const confirmBtn = document.getElementById('btn-confirm-import');
        if (confirmBtn) {
            confirmBtn.disabled = false;
        }
    }
}

/**
 * Import models from JSON file
 */
async function handleImportModels() {
    const fileInput = document.getElementById('import-file-input');
    const file = fileInput.files[0];
    
    if (!file) {
        showToast('Please select a JSON file first', 'error');
        return;
    }

    try {
        const content = await file.text();
        const modelsData = JSON.parse(content);
        
        // Validate the structure - should have a 'models' object
        if (!modelsData.models || typeof modelsData.models !== 'object') {
            showToast('Invalid JSON format. Expected { "models": {...} }', 'error');
            return;
        }

        // Send to server for import
        const response = await fetch('/api/models/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models: modelsData.models })
        });
        
        const result = await response.json();
        
        if (result.success) {
            showToast(`Imported ${result.count} model(s) successfully!`, 'success');
            closeModal('import-models-overlay');
            
            // Reset file input and clear selection text
            fileInput.value = '';
            const selectedText = document.getElementById('import-file-selected');
            if (selectedText) {
                selectedText.textContent = '';
            }
            
            // Disable import button
            const confirmBtn = document.getElementById('btn-confirm-import');
            if (confirmBtn) {
                confirmBtn.disabled = true;
            }
            
            // Refresh system state
            await fetchSystemState(true);
        } else {
            showToast(result.message || 'Import failed', 'error');
        }
    } catch (error) {
        if (error instanceof SyntaxError) {
            showToast('Invalid JSON file. Please check the file format.', 'error');
        } else {
            showToast('Import failed: ' + error.message, 'error');
        }
    }
}

// ========================================= //
// PIN Security System                       //
// ========================================= //

const PIN_STORAGE_KEY = 'llamashift_pin';
const PIN_SESSION_KEY = 'llamashift_pin_session'; // Timestamp of last successful verification
const PIN_SESSION_DURATION = 24 * 60 * 60 * 1000; // 24 hours in milliseconds

const PinSecurity = {
    currentPin: '',
    confirmedPin: '',
    maxDigits: 6,

    /**
     * Check if PIN exists
     */
    hasPin: function() {
        return localStorage.getItem(PIN_STORAGE_KEY) !== null;
    },

    /**
     * Get stored PIN hash
     */
    getStoredPin: function() {
        return localStorage.getItem(PIN_STORAGE_KEY);
    },

    /**
     * Save PIN (stores hash)
     */
    savePin: async function(pin) {
        // Store as plain text for simplicity (6 digits is manageable)
        // In production, you should hash this with a salt
        localStorage.setItem(PIN_STORAGE_KEY, pin);
        return true;
    },

    /**
     * Check PIN validity and update session
     */
    checkPinSecurity: async function() {
        if (!this.isSessionValid()) {
            const storedPin = this.getStoredPin();
            if (storedPin) {
                try {
                    const response = await fetch('/api/verify-pin', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({
                            pin: storedPin
                        })
                    });

                    if (response.ok) {
                        this.updateSession();
                    } else {
                        this.clearPin();
                    }
                } catch (error) {
                    console.error('Security check failed:', error);
                    this.clearPin();
                }
            }
        }
        return this.isSessionValid();
    },

    /**
     * Check if session is still valid (within 24 hours)
     */
    isSessionValid: function() {
        const lastSessionTime = localStorage.getItem(PIN_SESSION_KEY);
        if (!lastSessionTime) return false;
        const now = Date.now();
        return (now - lastSessionTime) < PIN_SESSION_DURATION;
    },

    /**
     * Update session timestamp
     */
    updateSession: function() {
        localStorage.setItem(PIN_SESSION_KEY, Date.now());
    },

    /**
     * Clear PIN (for debugging/testing)
     */
    clearPin: function() {
        localStorage.removeItem(PIN_STORAGE_KEY);
        localStorage.removeItem(PIN_SESSION_KEY);
    },

};

/**
 * Handle PIN verification
 */
async function handlePinVerification() {
    // Get the PIN from the display (since we're using display digits, not a text input)
    const pin = PinSecurity.currentPin;
    const errorEl = document.getElementById('pin-verify-error');
    
    if (!pin) {
        if (errorEl) {
            errorEl.textContent = 'Please enter your PIN';
            errorEl.style.display = 'block';
        }
        return;
    }
    
    // Show loading state
    const verifyBtn = document.getElementById('pin-verify-done-btn');
    const originalBtnText = verifyBtn.innerHTML;
    verifyBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Verifying...';
    verifyBtn.disabled = true;
    
    try {
        const response = await fetch('/api/verify-pin', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                pin: pin
            })
        });
        
        const data = await response.json();
        
        if (response.ok && data.success) {
            // Store PIN in session storage
            localStorage.setItem('llamashift_pin_session', pin);
            // Clear any PIN state from memory - this is the key fix
            PinSecurity.currentPin = '';
            PinSecurity.confirmedPin = '';
            // Redirect to dashboard
            window.location.href = '/dashboard';
        } else {
            if (errorEl) {
                errorEl.textContent = data.message || 'Invalid PIN';
                errorEl.style.display = 'block';
            }
            // Clear the PIN display
            verifyDisplay.forEach(d => {
                d.textContent = '';
                d.classList.remove('filled');
            });
            PinSecurity.currentPin = '';
        }
    } catch (error) {
        console.error('PIN verification error:', error);
        if (errorEl) {
            errorEl.textContent = 'Network error. Please try again.';
            errorEl.style.display = 'block';
        }
    } finally {
        // Restore button state
        verifyBtn.innerHTML = originalBtnText;
        verifyBtn.disabled = false;
    }
}

/**
 * Show the appropriate PIN screen on page load
 */
function showInitialPinScreen() {
    if (PinSecurity.hasPin() && PinSecurity.isSessionValid()) {
        // Already verified, skip to dashboard
        window.location.href = '/dashboard';
    } else if (PinSecurity.hasPin()) {
        showVerifyPinScreen();
    } else {
        showCreatePinScreen();
    }
}

/**
 * Setup keypad button event listeners
 */
function setupKeypadListeners() {
    // Use event delegation for all keypad buttons
    document.addEventListener('click', function(e) {
        if (e.target.classList.contains('pin-key')) {
            const button = e.target;
            const digit = button.getAttribute('data-key') || button.textContent.trim();
            const action = button.getAttribute('data-action');
            
            // Get current mode from the DOM
            const currentMode = getCurrentPinMode();
            
            // Handle clear action
            if (action === 'clear') {
                // Clear current PIN in memory
                if (currentMode === 'create') {
                    PinSecurity.currentPin = '';
                } else if (currentMode === 'confirm') {
                    PinSecurity.confirmedPin = '';
                } else if (currentMode === 'verify') {
                    PinSecurity.currentPin = '';
                }
                
                // Clear display
                if (currentMode === 'create') {
                    createDisplay.forEach(d => {
                        d.textContent = '';
                        d.classList.remove('filled');
                    });
                } else if (currentMode === 'confirm') {
                    confirmDisplay.forEach(d => {
                        d.textContent = '';
                        d.classList.remove('filled');
                    });
                } else if (currentMode === 'verify') {
                    verifyDisplay.forEach(d => {
                        d.textContent = '';
                        d.classList.remove('filled');
                    });
                }
                return;
            }
            
            // Handle done action
            if (action === 'done') {
                if (currentMode === 'create') {
                    handleCreatePin();
                } else if (currentMode === 'confirm') {
                    handleConfirmPin();
                } else if (currentMode === 'verify') {
                    handlePinVerification();
                }
                return;
            }
            
            // Only process numeric digits
            if (digit && !isNaN(digit) && digit !== '') {
                // Handle digit input based on current mode
                if (currentMode === 'create') {
                    // Add digit to create display
                    const emptySlotIndex = Array.from(createDisplay).findIndex(d => !d.textContent);
                    if (emptySlotIndex !== -1) {
                        createDisplay[emptySlotIndex].textContent = digit;
                        createDisplay[emptySlotIndex].classList.add('filled');
                        PinSecurity.currentPin += digit;
                    }
                } else if (currentMode === 'confirm') {
                    // Add digit to confirm display
                    const emptySlotIndex = Array.from(confirmDisplay).findIndex(d => !d.textContent);
                    if (emptySlotIndex !== -1) {
                        confirmDisplay[emptySlotIndex].textContent = digit;
                        confirmDisplay[emptySlotIndex].classList.add('filled');
                        PinSecurity.confirmedPin += digit;
                    }
                } else if (currentMode === 'verify') {
                    // Add digit to verify display
                    const emptySlotIndex = Array.from(verifyDisplay).findIndex(d => !d.textContent);
                    if (emptySlotIndex !== -1) {
                        verifyDisplay[emptySlotIndex].textContent = digit;
                        verifyDisplay[emptySlotIndex].classList.add('filled');
                        PinSecurity.currentPin += digit;
                    }
                }
            }
        }
    });
}


/**
 * Handle clear input
 */
function handleClearInput() {
    const currentMode = getCurrentPinMode();

    if (currentMode === 'create') {
        // Remove last digit
        const digits = Array.from(createDisplay);
        for (let i = digits.length - 1; i >= 0; i--) {
            if (digits[i].textContent) {
                digits[i].textContent = '';
                digits[i].classList.remove('filled');
                PinSecurity.currentPin = PinSecurity.currentPin.slice(0, -1);
                break;
            }
        }
    } else if (currentMode === 'confirm') {
        const digits = Array.from(confirmDisplay);
        for (let i = digits.length - 1; i >= 0; i--) {
            if (digits[i].textContent) {
                digits[i].textContent = '';
                digits[i].classList.remove('filled');
                PinSecurity.confirmedPin = PinSecurity.confirmedPin.slice(0, -1);
                break;
            }
        }
    } else if (currentMode === 'verify') {
        const digits = Array.from(verifyDisplay);
        for (let i = digits.length - 1; i >= 0; i--) {
            if (digits[i].textContent) {
                digits[i].textContent = '';
                digits[i].classList.remove('filled');
                PinSecurity.currentPin = PinSecurity.currentPin.slice(0, -1);
                break;
            }
        }
    }
}

/**
 * Handle done input (submit PIN)
 */
async function handleDoneInput() {
    const currentMode = getCurrentPinMode();

    if (currentMode === 'create') {
        if (PinSecurity.currentPin.length !== 6) {
            showToast('PIN must be exactly 6 digits', 'error');
            return;
        }
        showConfirmPinScreen();
    } else if (currentMode === 'confirm') {
        if (PinSecurity.confirmedPin.length !== 6) {
            showPinError('pin-confirm-section', 'Please confirm your 6-digit PIN');
            return;
        }
        if (PinSecurity.currentPin === PinSecurity.confirmedPin) {
            PinSecurity.savePin(PinSecurity.currentPin);
            showToast('Security PIN created successfully!', 'success');
            showVerifyPinScreen();
        } else {
            showPinError('pin-confirm-section', 'PINs do not match. Please try again.');
            // Reset confirmation input
            PinSecurity.confirmedPin = '';
            confirmDisplay.forEach(d => {
                d.textContent = '';
                d.classList.remove('filled');
            });
        }
    } else if (currentMode === 'verify') {
        if (PinSecurity.currentPin.length !== 6) {
            showPinError('pin-verify-section', 'Please enter your 6-digit PIN');
            return;
        }
        
        // Show loading state
        const verifyOverlay = document.getElementById('pin-security-overlay');
        const doneBtn = document.querySelector('[data-action="done"]');
        if (doneBtn) {
            doneBtn.textContent = 'Verifying...';
            doneBtn.disabled = true;
        }
        
        const isVerified = await PinSecurity.verifyPin(PinSecurity.currentPin);
        
        // Restore button
        if (doneBtn) {
            doneBtn.textContent = 'DONE';
            doneBtn.disabled = false;
        }
        
        if (isVerified) {
            // PIN correct - allow access
            PinSecurity.updateSession(); // Update session timestamp for 24-hour validity
            hidePinOverlay();
        } else {
            showPinError('pin-verify-section', 'Incorrect PIN. Please try again.');
            // Add error styling to digits
            verifyDisplay.forEach(d => d.classList.add('error'));
            setTimeout(() => {
                PinSecurity.currentPin = '';
                verifyDisplay.forEach(d => {
                    d.textContent = '';
                    d.classList.remove('filled');
                    d.classList.remove('error');
                });
            }, 500);
        }
    }
}

/**
 * Get current PIN mode
 */
function getCurrentPinMode() {
    if (document.getElementById('pin-create-section').classList.contains('active')) {
        return 'create';
    } else if (document.getElementById('pin-confirm-section').classList.contains('active')) {
        return 'confirm';
    } else if (document.getElementById('pin-verify-section').classList.contains('active')) {
        return 'verify';
    }
    return null;
}

/**
 * Show error message
 */
function showPinError(elementId, message) {
    const errorEl = document.getElementById(elementId === 'pin-create-section' ? null : 
        elementId === 'pin-confirm-section' ? 'pin-confirm-error' : 'pin-verify-error');
    
    if (errorEl) {
        errorEl.textContent = message;
        errorEl.style.display = 'block';
    }
}

/**
 * Show Create PIN Screen
 */
function showCreatePinScreen() {
    // Reset PIN storage
    PinSecurity.currentPin = '';
    
    // Clear all displays
    createDisplay.forEach(d => {
        d.textContent = '';
        d.classList.remove('filled');
    });
    confirmDisplay.forEach(d => {
        d.textContent = '';
        d.classList.remove('filled');
    });
    verifyDisplay.forEach(d => {
        d.textContent = '';
        d.classList.remove('filled');
    });
    
    // Show create section, hide others
    document.getElementById('pin-create-section').classList.add('active');
    document.getElementById('pin-confirm-section').classList.remove('active');
    document.getElementById('pin-verify-section').classList.remove('active');
    
    // Show overlay
    pinOverlay.classList.remove('hidden');
    pinOverlay.classList.add('open');
}

/**
 * Show Confirm PIN Screen
 */
function showConfirmPinScreen() {
    // Hide create, show confirm
    document.getElementById('pin-create-section').classList.remove('active');
    document.getElementById('pin-confirm-section').classList.add('active');
    
    // Clear confirm display
    PinSecurity.confirmedPin = '';
    confirmDisplay.forEach(d => {
        d.textContent = '';
        d.classList.remove('filled');
    });
    
    // Remove error
    const errorEl = document.getElementById('pin-confirm-error');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
}

/**
 * Show Verify PIN Screen
 */
function showVerifyPinScreen() {
    // Hide create and confirm, show verify
    document.getElementById('pin-create-section').classList.remove('active');
    document.getElementById('pin-confirm-section').classList.remove('active');
    document.getElementById('pin-verify-section').classList.add('active');
    
    // Clear verify display
    PinSecurity.currentPin = '';
    verifyDisplay.forEach(d => {
        d.textContent = '';
        d.classList.remove('filled');
    });
    
    // Remove error
    const errorEl = document.getElementById('pin-verify-error');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
    
    // Show overlay
    pinOverlay.classList.remove('hidden');
    pinOverlay.classList.add('open');
}

/**
 * Hide PIN overlay
 */
function hidePinOverlay() {
    pinOverlay.classList.remove('open');
    setTimeout(() => {
        pinOverlay.classList.add('hidden');
    }, 300);
}

// ==============================
// PIN Security Initialization
// ==============================

/**
 * Initialize PIN security system
 */
function initPinSecurity() {
    setupKeypadListeners();
    PinSecurity.checkPinSecurity();
}

// ==============================
// Reset PIN Modal Functions
// ==============================

const resetPinOverlay = document.getElementById('reset-pin-overlay');

/**
 * Show Reset PIN modal
 */
function showResetPinModal() {
    // Clear inputs
    document.getElementById('reset-new-pin-input').value = '';
    document.getElementById('reset-confirm-pin-input').value = '';
    
    // Clear error message
    const errorEl = document.getElementById('reset-pin-error');
    if (errorEl) {
        errorEl.textContent = '';
        errorEl.style.display = 'none';
    }
    
    resetPinOverlay.classList.remove('hidden');
    resetPinOverlay.classList.add('open');
}

/**
 * Hide Reset PIN modal
 */
function hideResetPinModal() {
    resetPinOverlay.classList.remove('open');
    setTimeout(() => {
        resetPinOverlay.classList.add('hidden');
    }, 300);
}

/**
 * Handle Reset PIN submission
 */
async function handleResetPin() {
    const newPin = document.getElementById('reset-new-pin-input').value.trim();
    const confirmPin = document.getElementById('reset-confirm-pin-input').value.trim();
    const errorEl = document.getElementById('reset-pin-error');
    
    // Validation
    if (!newPin || !confirmPin) {
        if (errorEl) {
            errorEl.textContent = 'Please enter and confirm your new PIN';
            errorEl.style.display = 'block';
        }
        return;
    }
    
    if (newPin.length < 4) {
        if (errorEl) {
            errorEl.textContent = 'PIN must be at least 4 characters long';
            errorEl.style.display = 'block';
        }
        return;
    }
    
    if (newPin !== confirmPin) {
        if (errorEl) {
            errorEl.textContent = 'PINs do not match';
            errorEl.style.display = 'block';
        }
        return;
    }
    
    // Show loading state
    const confirmBtn = document.getElementById('btn-confirm-reset-pin');
    const originalBtnText = confirmBtn.innerHTML;
    confirmBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Resetting...';
    confirmBtn.disabled = true;
    
    try {
        const response = await fetch('/api/admin/reset-pin', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                newPin: newPin
            })
        });
        
        const data = await response.json();
        
         if (response.ok && data.success) {
             // Show success message
             showToast('PIN reset successfully!', 'success');
             
             // Clear session PIN so user has to re-login with new PIN
             localStorage.removeItem('llamashift_pin_session');
             
             // Clear in-memory PIN state to prevent incorrect display
             PinSecurity.currentPin = '';
             PinSecurity.confirmedPin = '';
             
             // Close modal
             hideResetPinModal();
             
             // Show PIN overlay for new login
             setTimeout(() => {
                 showVerifyPinScreen();
             }, 1000);
         } else {
             if (errorEl) {
                 errorEl.textContent = data.message || 'Failed to reset PIN';
                 errorEl.style.display = 'block';
             }
         }
    } catch (error) {
        console.error('Reset PIN error:', error);
        if (errorEl) {
            errorEl.textContent = 'Network error. Please try again.';
            errorEl.style.display = 'block';
        }
    } finally {
        confirmBtn.innerHTML = originalBtnText;
        confirmBtn.disabled = false;
    }
}

// Event Listeners for Reset PIN
// Reset PIN button
const btnResetPin = document.getElementById('btn-reset-pin');
if (btnResetPin) {
    btnResetPin.addEventListener('click', showResetPinModal);
}

// Close buttons
const btnCloseResetPin = document.getElementById('btn-close-reset-pin');
const btnCancelResetPin = document.getElementById('btn-cancel-reset-pin');

if (btnCloseResetPin) {
    btnCloseResetPin.addEventListener('click', hideResetPinModal);
}

if (btnCancelResetPin) {
    btnCancelResetPin.addEventListener('click', hideResetPinModal);
}

// Confirm button
const btnConfirmResetPin = document.getElementById('btn-confirm-reset-pin');
if (btnConfirmResetPin) {
    btnConfirmResetPin.addEventListener('click', handleResetPin);
}

// Close on outside click
if (resetPinOverlay) {
    resetPinOverlay.addEventListener('click', (e) => {
        if (e.target === resetPinOverlay) {
            hideResetPinModal();
        }
    });
}

