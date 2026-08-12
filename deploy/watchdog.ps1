# Keeps the three always-on services alive without a human noticing they died
# first. Runs on a repeating scheduled task (every 3 minutes) rather than
# ONSTART-only, because ONSTART only fires at boot -- it does nothing for a
# process that gets killed mid-day by a deploy, a manual restart, or someone
# working on the box for an unrelated reason. That gap is exactly what caused
# a multi-hour outage of jobs.skarion.com and llm.skarion.com on 2026-08-12:
# cloudflared and Streamlit were both killed, and nothing brought them back
# until a human noticed and SSH'd in.
#
# Checks by the actual signal that matters -- a listening port for the two
# local servers, process existence for cloudflared (which holds an outbound
# tunnel, not a local listener) -- and restarts only what's actually missing,
# via the existing scheduled tasks so there is exactly one launch path per
# service, not a second copy of the launch logic living here too.
#
# Registered as: schtasks /Create /TN Watchdog /TR "powershell -ExecutionPolicy Bypass -File C:\JobSearch-Support-Talentos\deploy\watchdog.ps1" /SC MINUTE /MO 3 /RU <user> /F

$Root = "C:\JobSearch-Support-Talentos"
$LogPath = "$Root\watchdog.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $LogPath -Value $line
}

function PortListening($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# --- cloudflared: no local port, check by process name ---
$cf = Get-Process cloudflared -ErrorAction SilentlyContinue
if (-not $cf) {
    Log "cloudflared NOT running -- restarting CloudflareTunnel task"
    schtasks /Run /TN CloudflareTunnel | Out-Null
}

# --- Streamlit app: port 3100 ---
if (-not (PortListening 3100)) {
    Log "port 3100 not listening (JobSearchApp) -- restarting JobSearchApp task"
    schtasks /Run /TN JobSearchApp | Out-Null
}

# --- LLM gateway: port 8787 ---
if (-not (PortListening 8787)) {
    Log "port 8787 not listening (LLMGateway) -- restarting LLMGateway task"
    schtasks /Run /TN LLMGateway | Out-Null
}

# Trim the log so it never grows unbounded -- keep the last 2000 lines.
if (Test-Path $LogPath) {
    $lines = Get-Content $LogPath
    if ($lines.Count -gt 2000) {
        $lines[-2000..-1] | Set-Content $LogPath
    }
}
