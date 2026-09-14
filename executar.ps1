# Executa o monitor em modo continuo. Passe argumentos extras normalmente:
#   ./executar.ps1 --once
#   ./executar.ps1 --intervalo 30
Set-Location -Path $PSScriptRoot
python "$PSScriptRoot\monitor_passagens.py" @args
