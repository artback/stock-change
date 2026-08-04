# Telegram bot for the portfolio, for the Raspberry Pi Nomad cluster.
#
# Secrets live in a Nomad Variable, not in this file, so the spec can be
# committed and read without handing over the bot token:
#
#   nomad var put nomad/jobs/stock-bot \
#     telegram_token=<token from @BotFather> \
#     allowed_chat_ids=<your numeric chat id>
#
# Don't know your chat id? Set allowed_chat_ids="" for the first run, message
# the bot, and it replies with the id. It serves no portfolio data until the
# allowlist is filled in.
#
#   nomad job run deploy/nomad/stock-bot.nomad.hcl
#
# The bot is read-only: it reports the portfolio and cannot edit holdings or
# place trades.

job "stock-bot" {
  datacenters = ["kalmar"]
  type        = "service"

  group "bot" {
    count = 1

    restart {
      attempts = 3
      interval = "10m"
      delay    = "30s"
      mode     = "delay"
    }

    task "bot" {
      driver = "docker"

      config {
        # Debian rather than Alpine: pandas and numpy publish manylinux
        # aarch64 wheels for glibc, but on musl pip builds them from source,
        # which takes the better part of an hour on a Pi.
        image   = "python:3.12-slim"
        command = "/bin/sh"
        args    = ["-c", "/local/start.sh"]

        mounts = [{
          type     = "volume"
          source   = "stock-bot-data"
          target   = "/data"
          readonly = false
        }]
      }

      env {
        # Portfolio history and the price cache live under $HOME, so pointing
        # it at the volume is what lets "30D PORTFOLIO" accumulate across
        # restarts instead of resetting every deploy.
        HOME               = "/data"
        STOCK_PRICE_CONFIG = "/local/stock.yaml"
        PIP_ROOT_USER_ACTION = "ignore"
      }

      template {
        destination = "secrets/bot.env"
        env         = true
        change_mode = "restart"
        # allowed_chat_ids is read with `index` so a missing key renders empty
        # rather than failing the template. That is the first-run case: the bot
        # comes up in setup mode, tells you your chat id, and serves no
        # portfolio data until you add it here.
        data        = <<-EOT
          {{ with nomadVar "nomad/jobs/stock-bot" }}
          TELEGRAM_TOKEN={{ .telegram_token }}
          TELEGRAM_ALLOWED_CHAT_IDS={{ index . "allowed_chat_ids" }}
          {{ end }}
        EOT
      }

      # Holdings are personal data; keeping them in a template rather than the
      # image means they are changed with a job update, not a rebuild.
      template {
        destination = "local/stock.yaml"
        change_mode = "restart"
        data        = <<-EOT
          holdings:
            SVOL-B.ST: 8367
            INVE-B.ST: 1387
            LIFCO-B.ST: 5
            MC.PA: 84
            INDU-C.ST: 25
            IUSA.DE: 720
            ETZD.PA: 4172
            ESE.PA: 803
          currency: EUR
        EOT
      }

      template {
        destination = "local/start.sh"
        perms       = "755"
        change_mode = "restart"
        data        = <<-EOT
          #!/bin/sh
          set -eu
          # Installed from the release tarball rather than git+https: the slim
          # image has no git, and a tarball avoids installing one just to fetch
          # a few files. Pinned to a tag so an unattended restart can't pull an
          # untested main.
          pip install --no-cache-dir --quiet \
            "https://github.com/artback/stock-change/archive/refs/tags/v0.9.0.tar.gz"
          exec stock-price-bot
        EOT
      }

      resources {
        cpu    = 500
        memory = 384
      }
    }
  }
}
