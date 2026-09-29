# Monitor de passagens AJU -> GRU

Monitora o preco da passagem de Aracaju (AJU) para Guarulhos (GRU), ida e volta,
1 adulto, ida em 24/04/2027 e volta em 26/04/2027, e avisa no Telegram quando
aparecer alguma opcao abaixo de R$ 1.000.

Le os precos do Google Flights e usa apenas a biblioteca padrao do Python 3.10+.
Nao precisa instalar nada.

## 1. Criar o bot do Telegram

1. No Telegram, fale com o **@BotFather** e envie `/newbot`.
2. Escolha um nome e um usuario para o bot. Ele devolve um token parecido com
   `123456789:AAH...`.
3. Envie qualquer mensagem para o seu bot (por exemplo `/start`) para abrir a conversa.
4. Descubra o seu `chat_id` abrindo no navegador:
   `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` e procure por
   `"chat":{"id":123456789`.

## 2. Configurar

Edite `config.json`:

```json
{
  "origem": "AJU",
  "destino": "GRU",
  "data_ida": "2027-04-24",
  "data_volta": "2027-04-26",
  "adultos": 1,
  "classe": "economica",
  "preco_maximo": 1200,
  "intervalo_minutos": 60,
  "reenviar_apos_horas": 12,
  //"telegram_bot_token": "8844378729:AAGjwT-ySsOv3x7igC6HgRrS-HIg0A89h8o",
  //"telegram_chat_id": "507436072"
  "telegram_bot_token": "8861952248:AAGZ1NbuqJrly_NZZGn3eRTsNxLJT-gv60Y",
  "telegram_chat_id": "8861952248"
}
```

Se preferir nao guardar o token no arquivo, use as variaveis de ambiente
`TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, que tem prioridade sobre o `config.json`.

Confirme o envio:

```powershell
python monitor_passagens.py --testar-telegram
```

## 3. Rodar

Uma consulta unica:

```powershell
python monitor_passagens.py --once
```

Modo continuo, consultando a cada 60 minutos:

```powershell
python monitor_passagens.py
```

Ou use `executar.ps1`, que so chama o script na pasta certa.

Opcoes uteis:

| Opcao | Efeito |
| --- | --- |
| `--once` | uma consulta e sai (ideal para o Agendador de Tarefas) |
| `--intervalo 30` | 30 minutos entre consultas no modo continuo |
| `--preco-maximo 900` | muda o limite do alerta |
| `--sem-notificar` | so consulta e mostra os precos, sem Telegram |
| `--previa` | consulta agora e manda o alerta no Telegram, mesmo acima do limite |
| `--origem` `--destino` `--data-ida` `--data-volta` `--adultos` | sobrescrevem o `config.json` |
| `--verbose` | mostra a URL consultada e mais detalhes |

## 4. Agendar no Windows

Rodar a cada hora sem manter o terminal aberto:

```powershell
./agendar-tarefa.ps1
```

Isso cria a tarefa `MonitorPassagensAJU-GRU` no Agendador de Tarefas, executando
`--once` de hora em hora. Para remover:

```powershell
Unregister-ScheduledTask -TaskName "MonitorPassagensAJU-GRU" -Confirm:$false
```

## Como o alerta se comporta

- Alerta quando o menor preco da viagem fica **abaixo** de `preco_maximo`. Esse
  preco e o menor entre o total de ida e volta comprado junto e a soma dos dois
  trechos comprados separados.
- A mensagem abre com o bloco **PRECOS**, que separa o valor da ida, o da volta,
  a soma dos dois e o total de ida e volta comprando junto. Cada valor vem com a
  companhia e o horario do voo mais barato daquele trecho.
- Depois vem as tres melhores opcoes de **ida** e as tres de **volta**, cada uma
  com companhia, horarios, duracao, paradas, o preco do trecho avulso e o total
  de ida e volta com aquele voo.
- Cada verificacao faz quatro consultas: a lista de ida e a lista de volta da
  pesquisa de ida e volta, que dao o total da viagem, e uma pesquisa de so ida
  para cada trecho, que da o preco separado de cada um.
- Nao repete o mesmo aviso a cada consulta: so reenvia se o preco cair mais ainda
  ou depois de `reenviar_apos_horas` horas.
- O historico fica em `estado.json` (`menor_preco_ida`, `menor_preco_volta`,
  `menor_total`, `melhores_ida` e `melhores_volta`) e o log em `monitor.log`.

## Limitacoes

- Os precos vem da pagina publica do Google Flights. Se o Google mudar o HTML ou
  bloquear as consultas, o script registra `Nenhum preco extraido` no log. Nesse
  caso o seletor em `parse_voos` precisa ser ajustado.
- Intervalos muito curtos aumentam a chance de bloqueio. Como agora sao quatro
  requisicoes por verificacao, uma consulta por hora e um bom limite; evite menos
  de 30 minutos.
- Precos sao para o total de passageiros configurado, sem bagagem despachada, e
  podem variar na hora da compra.
- O Google nao permite indicar por URL que a ida ja foi escolhida, entao o script
  nao consegue confirmar o preco de um par especifico de ida + volta. Combinar a
  ida mais barata com a volta mais barata pode dar um total um pouco diferente;
  confira no link antes de comprar.
