# ProxyLane 网关连接监视器
# 每 60 秒记录所有通向 ProxyLane 网关的连接（含进程名），写入 logs\proxylane-watch.log
# 用途：流量异常时对照日志，确认消耗是否来自本机
$logFile = "D:\Tools\GPT无限生图\GPT-Web-Registration-Full-Workflow\logs\proxylane-watch.log"

while ($true) {
  try {
    $realIPs = @()
    try {
      # DoH 查真实 IP，绕过本地 Fake-IP DNS
      $resp = Invoke-RestMethod -Uri "https://1.1.1.1/dns-query?name=asia.gw.proxylane.dev&type=A" -Headers @{accept='application/dns-json'} -TimeoutSec 8
      $realIPs = @($resp.Answer | Where-Object type -eq 1 | ForEach-Object data)
    } catch {}
    if (-not $realIPs) { $realIPs = @('178.249.213.83') }

    $hits = Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {
      ($realIPs -contains $_.RemoteAddress) -or ($_.RemotePort -in 10000..10009)
    }
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    if ($hits) {
      foreach ($h in $hits) {
        $p = Get-Process -Id $h.OwningProcess -ErrorAction SilentlyContinue
        Add-Content -Path $logFile -Value "$ts | $($h.RemoteAddress):$($h.RemotePort) | 进程=$($p.ProcessName)(PID $($h.OwningProcess)) | $($h.State)"
      }
    } else {
      Add-Content -Path $logFile -Value "$ts | 无网关连接"
    }
  } catch {
    Add-Content -Path $logFile -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | 监视器异常: $_"
  }
  Start-Sleep -Seconds 60
}
