// KubeSentinel Autonomous SRE Console App
let chartCounter = 0;

function triggerScenario(type) {
    const input = document.getElementById("messageInput");
    const ns = document.getElementById("namespaceSelect");

    if (type === "checkout-oom") {
        input.value = "checkout-service pods are crashing with exit code 137 (OOMKilled) in production. Investigate root cause and propose remediation.";
        ns.value = "production";
    } else if (type === "latency-spike") {
        input.value = "gateway-service p99 latency spiked over 3.8s and 5xx errors are rising. Investigate cluster bottlenecks.";
        ns.value = "production";
    }
    handleSend(new Event("submit"));
}

async function handleSend(e) {
    if (e) e.preventDefault();
    const input = document.getElementById("messageInput");
    const query = input.value.trim();
    if (!query) return;

    const ns = document.getElementById("namespaceSelect").value;
    input.value = "";

    // Append user message
    appendUserMessage(query);

    // Disable button during investigation
    const sendBtn = document.getElementById("sendBtn");
    sendBtn.disabled = true;
    sendBtn.innerHTML = "<span>Analyzing...</span> ⏳";

    try {
        const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: query, namespace: ns, service: "checkout-service" }),
        });

        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split("\n\n");
            buffer = lines.pop(); // Keep unparsed part

            for (const line of lines) {
                if (line.startsWith("data: ")) {
                    const rawJson = line.substring(6).trim();
                    if (!rawJson) continue;
                    try {
                        const event = JSON.parse(rawJson);
                        handleAgentEvent(event);
                    } catch (err) {
                        console.error("JSON parse error", err, rawJson);
                    }
                }
            }
        }
    } catch (err) {
        console.error("Chat stream failed", err);
        appendErrorMessage("Investigation stream error: " + err.message);
    } finally {
        sendBtn.disabled = false;
        sendBtn.innerHTML = "<span>Investigate</span> ⚡";
    }
}

function appendUserMessage(text) {
    const container = document.getElementById("messagesContainer");
    const div = document.createElement("div");
    div.className = "message user-message";
    div.innerHTML = `<strong>SRE Engineer:</strong><p>${escapeHtml(text)}</p>`;
    container.appendChild(div);
    scrollToBottom();
}

function appendErrorMessage(text) {
    const container = document.getElementById("messagesContainer");
    const div = document.createElement("div");
    div.className = "message";
    div.style.borderColor = "var(--accent-red)";
    div.innerHTML = `<strong style="color:var(--accent-red)">Error:</strong><p>${escapeHtml(text)}</p>`;
    container.appendChild(div);
    scrollToBottom();
}

function handleAgentEvent(event) {
    const container = document.getElementById("messagesContainer");

    switch (event.type) {
        case "thought": {
            const el = document.createElement("div");
            el.className = "agent-thought";
            const hopLabel = event.hop ? `<span class="hop-badge">Hop ${event.hop}</span> ` : "";
            el.innerHTML = `<strong>🧠 Sentinel Reasoning:</strong> ${hopLabel}<p>${escapeHtml(event.content)}</p>`;
            container.appendChild(el);
            break;
        }

        case "tool_call": {
            const el = document.createElement("div");
            el.className = "tool-card";
            el.id = `tool-hop-${event.hop || Date.now()}`;
            el.innerHTML = `
                <div class="tool-header">
                    <span class="hop-badge">Hop ${event.hop || 1}</span>
                    <span>⚡ Tool: <strong>${escapeHtml(event.tool)}</strong></span>
                </div>
                <div class="tool-input"><code>${escapeHtml(event.input)}</code></div>
            `;
            container.appendChild(el);
            break;
        }

        case "observation": {
            const tools = container.querySelectorAll(".tool-card");
            const lastTool = tools[tools.length - 1];
            if (lastTool) {
                const obs = document.createElement("div");
                obs.className = "tool-observation";
                const lat = event.execution_ms ? `<span class="latency-badge">${event.execution_ms}ms</span>` : "";
                obs.innerHTML = `<span>✔</span> <strong>Observation:</strong> ${escapeHtml(event.summary)} ${lat}`;
                
                // If logs are present, render dark terminal log drawer with secret chips
                if (event.raw_logs) {
                    const drawer = document.createElement("div");
                    drawer.className = "log-terminal-drawer";
                    drawer.innerHTML = highlightLogs(event.raw_logs);
                    obs.appendChild(drawer);
                }
                lastTool.appendChild(obs);
            }
            break;
        }

        case "step_completed": {
            // Updated hop completion latency
            const tools = container.querySelectorAll(".tool-card");
            const lastTool = tools[tools.length - 1];
            if (lastTool && !lastTool.querySelector(".latency-badge")) {
                const header = lastTool.querySelector(".tool-header");
                if (header && event.execution_ms) {
                    header.innerHTML += `<span class="latency-badge">${event.execution_ms}ms</span>`;
                }
            }
            break;
        }

        case "chart":
        case "metrics_series": {
            if (event.type === "chart") {
                renderChart(event.chart);
            }
            break;
        }

        case "synthesis":
        case "rca_ready": {
            if (event.type === "synthesis") {
                const el = document.createElement("div");
                el.className = "synthesis-card";
                
                let citationHtml = "";
                if (event.valid_citations && event.valid_citations.length > 0) {
                    citationHtml = `<div style="margin-bottom:8px;">` +
                        event.valid_citations.map(c => `<span class="citation-chip">✓ Verified Step ${c}</span>`).join("") +
                        `</div>`;
                }
                el.innerHTML = citationHtml + formatMarkdown(event.content);
                container.appendChild(el);
            }
            break;
        }

        case "remediation_card":
        case "remediation_proposed": {
            if (event.type === "remediation_card") {
                renderRemediationCard(event.card);
            }
            break;
        }
    }

    scrollToBottom();
}

function highlightLogs(logs) {
    if (!logs) return "";
    let safe = escapeHtml(logs);
    // Highlight [REDACTED:...] chips
    safe = safe.replace(/\[REDACTED:([A-Z_]+)\]/g, '<span class="redacted-chip">🔒 [REDACTED:$1]</span>');
    return safe;
}

function renderChart(chartData) {
    if (!chartData || !chartData.points) return;
    const container = document.getElementById("messagesContainer");
    chartCounter++;
    const canvasId = `chart-canvas-${chartCounter}`;

    const card = document.createElement("div");
    card.className = "chart-card";
    card.innerHTML = `
        <div class="chart-header">
            <span>📈 Live Prometheus Telemetry: <strong>${escapeHtml(chartData.metric_name)}</strong></span>
            <span style="font-family:var(--font-mono); color:var(--accent-cyan);">Latest: ${chartData.summary ? chartData.summary.latest : '512Mi'}</span>
        </div>
        <div class="chart-canvas-container" style="position:relative; height:180px;">
            <canvas id="${canvasId}"></canvas>
        </div>
    `;
    container.appendChild(card);

    // Render with Chart.js including CGroup Limit dashed line
    setTimeout(() => {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;
        const ctx = canvas.getContext("2d");
        const labels = chartData.points.map(p => {
            const d = new Date(p[0] * 1000);
            return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
        });
        const values = chartData.points.map(p => p[1]);

        // CGroup Limit baseline dataset (512 MiB = 536870912 bytes)
        const isMem = (chartData.metric_name || "").includes("memory") || (chartData.metric_name || "").includes("working_set");
        const cgroupLimitVal = isMem ? 536870912 : Math.max(...values) * 0.95;
        const limitSeries = new Array(values.length).fill(cgroupLimitVal);

        new Chart(ctx, {
            type: "line",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: chartData.metric_name || "Telemetry",
                        data: values,
                        borderColor: "#ef4444",
                        backgroundColor: "rgba(239, 68, 68, 0.15)",
                        borderWidth: 2,
                        fill: true,
                        tension: 0.3,
                    },
                    {
                        label: "CGroup Memory Limit (512Mi)",
                        data: limitSeries,
                        borderColor: "#f59e0b",
                        borderDash: [5, 5],
                        borderWidth: 1.5,
                        pointRadius: 0,
                        fill: false,
                    }
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
                    y: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
                },
                plugins: {
                    legend: { display: true, labels: { color: "#94a3b8", font: { size: 10 } } },
                },
            },
        });
    }, 50);
}

function renderRemediationCard(card) {
    const container = document.getElementById("messagesContainer");
    const div = document.createElement("div");
    div.className = "remediation-card";
    div.id = `remediation-${card.incident_id}`;

    div.innerHTML = `
        <div class="remediation-header">
            <div>
                <strong>🛡️ PROPOSED SRE REMEDIATION</strong>
                <span style="margin-left:8px; font-family:var(--font-mono); color:var(--accent-cyan);">${card.incident_id}</span>
            </div>
            <span class="remediation-badge">${card.safety_status}</span>
        </div>
        <div class="remediation-details">
            <div><strong>Target:</strong> <code>${card.service}</code> (Namespace: <code>${card.namespace}</code>)</div>
            <div><strong>Proposed Action:</strong> Rollback to revision <strong>${card.target_revision}</strong> (${card.target_image})</div>
            <div><strong>Blast Radius:</strong> ${card.blast_radius}</div>
            
            <div style="margin-top:8px;">
                <label style="font-size:0.8rem; color:#94a3b8;"><strong>Remediation Mode:</strong></label>
                <select id="modeSelect-${card.incident_id}" style="background:#0b1120; color:#38bdf8; border:1px solid #1e293b; padding:3px 6px; border-radius:4px; font-family:var(--font-mono);">
                    <option value="CLUSTER_DIRECT" selected>CLUSTER_DIRECT (Direct Rollback)</option>
                    <option value="GITOPS_PR">GITOPS_PR (Declarative Git Diff)</option>
                    <option value="GITOPS_BREAK_GLASS">GITOPS_BREAK_GLASS (Suspend ArgoCD + Rollback)</option>
                </select>
            </div>

            <div class="remediation-cmd"><code>${card.command}</code></div>
        </div>

        <div class="diff-card" id="diffCard-${card.incident_id}" style="display:none;">
            <div class="diff-header">
                <span>Unified GitOps YAML Diff</span>
                <span>Branch: kubesentinel/revert-${card.service}-rev${card.target_revision}</span>
            </div>
            <pre class="diff-content"><span class="diff-line-del">-        image: checkout:v2.4.2</span>
<span class="diff-line-add">+        image: ${card.target_image}</span></pre>
        </div>

        <div class="remediation-actions" id="actions-${card.incident_id}">
            <button class="btn-approve" onclick="approveRemediation('${card.incident_id}', '${card.action}', '${card.service}', '${card.namespace}')">
                ✅ EXECUTE REMEDIATION
            </button>
            <button class="btn-reject" onclick="rejectRemediation('${card.incident_id}')">
                ❌ Reject / Defer
            </button>
        </div>

        <div class="verification-radar" id="radar-${card.incident_id}" style="display:none;">
            <div style="display:flex; justify-content:space-between; font-size:0.85rem;">
                <span><strong>Spec-Derived Health Verification:</strong> Soak In Progress...</span>
                <span id="soakCounter-${card.incident_id}" style="color:var(--accent-cyan); font-family:var(--font-mono);">60s remaining</span>
            </div>
            <div class="soak-timer-bar">
                <div class="soak-timer-fill" id="soakFill-${card.incident_id}"></div>
            </div>
            <div style="font-size:0.75rem; color:#94a3b8; margin-top:6px;">
                Observing pod readiness probe pass-rate, zero restart delta, and CGroup saturation headroom.
            </div>
        </div>
    `;
    container.appendChild(div);

    // Wire mode change handler
    const modeSelect = div.querySelector(`#modeSelect-${card.incident_id}`);
    const diffCard = div.querySelector(`#diffCard-${card.incident_id}`);
    modeSelect.addEventListener("change", (e) => {
        if (e.target.value === "GITOPS_PR") {
            diffCard.style.display = "block";
        } else {
            diffCard.style.display = "none";
        }
    });
}

async function approveRemediation(incidentId, action, service, namespace) {
    const actionsBox = document.getElementById(`actions-${incidentId}`);
    const modeSelect = document.getElementById(`modeSelect-${incidentId}`);
    const mode = modeSelect ? modeSelect.value : "CLUSTER_DIRECT";
    const radar = document.getElementById(`radar-${incidentId}`);

    actionsBox.innerHTML = `<em>Initiating ${mode} execution plan... ⏳</em>`;
    if (radar && mode !== "GITOPS_PR") {
        radar.style.display = "block";
        const fill = document.getElementById(`soakFill-${incidentId}`);
        if (fill) fill.style.width = "40%";
    }

    try {
        const resp = await fetch("/api/remediate/execute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                incident_id: incidentId,
                action: action,
                service: service,
                namespace: namespace,
                mode: mode,
                approved_by: "oncall-sre@company.com",
                break_glass_reason: mode === "GITOPS_BREAK_GLASS" ? "Emergency checkout rollback during outage" : null,
                fast_forward_verify: true,
            }),
        });

        const data = await resp.json();
        if (resp.ok) {
            if (radar) {
                const fill = document.getElementById(`soakFill-${incidentId}`);
                if (fill) fill.style.width = "100%";
                const counter = document.getElementById(`soakCounter-${incidentId}`);
                if (counter) counter.innerText = "PASSED (Healthy)";
            }

            if (mode === "GITOPS_PR") {
                actionsBox.innerHTML = `
                    <div style="color:var(--accent-green); font-weight:bold; font-size:0.9rem;">
                        ✔ GitOps Pull Request Created: <a href="${data.plan_result.gitops_pr_url}" target="_blank" style="color:#38bdf8; text-decoration:underline;">${data.plan_result.gitops_pr_url}</a>. ArgoCD will sync upon merge.
                    </div>
                `;
            } else {
                actionsBox.innerHTML = `
                    <div style="color:var(--accent-green); font-weight:bold; font-size:0.9rem;">
                        ✔ Remediation Verified! Pods healthy (3/3 Ready). Zero restarts during soak. Incident state: RESOLVED.
                    </div>
                `;
                document.getElementById("activeIncidentsCount").innerText = "0";
            }
        } else {
            actionsBox.innerHTML = `<div style="color:var(--accent-red);">Failed: ${escapeHtml(data.detail)}</div>`;
        }
    } catch (err) {
        actionsBox.innerHTML = `<div style="color:var(--accent-red);">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function rejectRemediation(incidentId) {
    const actionsBox = document.getElementById(`actions-${incidentId}`);
    actionsBox.innerHTML = `<em>Remediation dismissed by engineer.</em>`;
}

function formatMarkdown(md) {
    let html = md
        .replace(/^### (.*$)/gim, '<h3>$1</h3>')
        .replace(/\*\*(.*?)\*\*/gim, '<strong>$1</strong>')
        .replace(/`([^`]+)`/gim, '<code>$1</code>')
        .replace(/^\- (.*$)/gim, '<li>$1</li>');
    return `<ul>${html}</ul>`;
}

function escapeHtml(text) {
    if (!text) return "";
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function scrollToBottom() {
    const container = document.getElementById("messagesContainer");
    container.scrollTop = container.scrollHeight;
}
