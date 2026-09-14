# Cria uma tarefa no Agendador de Tarefas do Windows que consulta os precos
# de hora em hora, em segundo plano, sem manter terminal aberto.

param(
    [int]$IntervaloHoras = 1,
    [string]$NomeTarefa = "MonitorPassagensAJU-GRU"
)

$python = (Get-Command python).Source
$script = Join-Path $PSScriptRoot "monitor_passagens.py"

if (-not (Test-Path $script)) {
    Write-Error "monitor_passagens.py nao encontrado em $PSScriptRoot"
    exit 1
}

$acao = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$script`" --once" -WorkingDirectory $PSScriptRoot

$gatilho = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervaloHoras)

$config = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $NomeTarefa -Action $acao -Trigger $gatilho `
    -Settings $config -Description "Monitora passagens AJU-GRU e avisa no Telegram" -Force

Write-Host "Tarefa '$NomeTarefa' criada: consulta a cada $IntervaloHoras hora(s)."
Write-Host "Para remover: Unregister-ScheduledTask -TaskName '$NomeTarefa' -Confirm:`$false"
