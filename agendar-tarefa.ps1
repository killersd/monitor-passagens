# Cria uma tarefa no Agendador de Tarefas do Windows que consulta os precos
# de hora em hora, em segundo plano, sem manter terminal aberto.
#
# Cada pasta do monitor precisa de um nome de tarefa proprio, senao uma
# tarefa sobrescreve a outra:
#   ./agendar-tarefa.ps1 -NomeTarefa "MonitorPassagens-Plinio"

param(
    [int]$IntervaloHoras = 1,
    [string]$NomeTarefa = "MonitorPassagensAJU-GRU"
)

$script = Join-Path $PSScriptRoot "monitor_passagens.py"

if (-not (Test-Path $script)) {
    Write-Error "monitor_passagens.py nao encontrado em $PSScriptRoot"
    exit 1
}

# Descobre o interpretador real pelo launcher "py" (o "python" do Windows pode
# ser so o atalho da Microsoft Store). Usa pythonw.exe para nao abrir janela.
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (& py -c "import sys; print(sys.executable)").Trim()
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (& python -c "import sys; print(sys.executable)").Trim()
}
if (-not $python -or -not (Test-Path $python)) {
    Write-Error "Python nao encontrado. Instale o Python ou o launcher 'py'."
    exit 1
}
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (Test-Path $pythonw) { $python = $pythonw }

$acao = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$script`" --once" -WorkingDirectory $PSScriptRoot

$gatilho = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervaloHoras)

$config = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho `
    -Settings $config -Description "Monitora passagens ($PSScriptRoot) e avisa no Telegram" -Force | Out-Null

Write-Host "Tarefa '$NomeTarefa' criada: consulta a cada $IntervaloHoras hora(s) usando $python."
Write-Host "Para remover: Unregister-ScheduledTask -TaskName '$NomeTarefa' -Confirm:`$false"
