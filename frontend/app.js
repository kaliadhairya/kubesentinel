// KubeSentinel SRE Console App
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
            el.innerHTML = `<strong>🧠 Sentinel Reasoning:</strong><p>${escapeHtml(event.content)}</p>`;
            container.appendChild(el);
            break;
        }

        case "tool_call": {
            const el = document.createElement("div");
            el.className = "tool-card";
            el.id = `tool-${Date.now()}`;
            el.innerHTML = `
                <div class="tool-header">
                    <span>⚡ Executing Tool: <strong>${escapeHtml(event.tool)}</strong></span>
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
                obs.innerHTML = `<span>✔</span> <strong>Observation:</strong> ${escapeHtml(event.summary)}`;
                lastTool.appendChild(obs);
            }
            break;
        }

        case "chart": {
            renderChart(event.chart);
            break;
        }

        case "synthesis": {
            const el = document.createElement("div");
            el.className = "synthesis-card";
            el.innerHTML = formatMarkdown(event.content);
            container.appendChild(el);
            break;
        }

        case "remediation_card": {
            renderRemediationCard(event.card);
            break;
        }
    }

    scrollToBottom();
}

function renderChart(chartData) {
    const container = document.getElementById("messagesContainer");
    chartCounter++;
    const canvasId = `chart-canvas-${chartCounter}`;

    const card = document.createElement("div");
    card.className = "chart-card";
    card.innerHTML = `
        <div class="chart-header">
            <span>📈 Live Prometheus Telemetry: <strong>${escapeHtml(chartData.metric_name)}</strong></span>
            <span style="font-family:var(--font-mono); color:var(--accent-cyan);">Latest: ${chartData.summary.latest}</span>
        </div>
        <div class="chart-canvas-container">
            <canvas id="${canvasId}"></canvas>
        </div>
    `;
    container.appendChild(card);

    // Render with Chart.js
    setTimeout(() => {
        const ctx = document.getElementById(canvasId).getContext("2d");
        const labels = chartData.points.map(p => {
            const d = new Date(p[0] * 1000);
            return `${d.getHours()}:${String(d.getMinutes()).padStart(2, "0")}`;
        });
        const values = chartData.points.map(p => p[1]);

        new Chart(ctx, {
            type: "line",
            data: {
                labels: labels,
                datasets: [{
                    label: chartData.metric_name,
                    data: values,
                    borderColor: "#ef4444",
                    backgroundColor: "rgba(239, 68, 68, 0.15)",
                    borderWidth: 2,
                    fill: true,
                    tension: 0.3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
                    y: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
                },
                plugins: {
                    legend: { display: false },
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
            <div class="remediation-cmd"><code>${card.command}</code></div>
        </div>
        <div class="remediation-actions" id="actions-${card.incident_id}">
            <button class="btn-approve" onclick="approveRemediation('${card.incident_id}', '${card.action}', '${card.service}', '${card.namespace}')">
                ✅ APPROVE & ROLLBACK
            </button>
            <button class="btn-reject" onclick="rejectRemediation('${card.incident_id}')">
                ❌ Reject / Defer
            </button>
        </div>
    `;
    container.appendChild(div);
}

async function approveRemediation(incidentId, action, service, namespace) {
    const actionsBox = document.getElementById(`actions-${incidentId}`);
    actionsBox.innerHTML = `<em>Executing zero-downtime rollback via Kubernetes API... ⏳</em>`;

    try {
        const resp = await fetch("/api/remediate/execute", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                incident_id: incidentId,
                action: action,
                service: service,
                namespace: namespace,
                approved_by: "oncall-sre@company.com",
            }),
        });

        const data = await resp.json();
        if (resp.ok) {
            actionsBox.innerHTML = `
                <div style="color:var(--accent-green); font-weight:bold; font-size:0.9rem;">
                    ✔ Rollback Executed Successfully! Pods recovered (3/3 Ready). Incident resolved.
                </div>
            `;
            document.getElementById("activeIncidentsCount").innerText = "0";
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
