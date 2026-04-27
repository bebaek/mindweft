#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL_DIR="$REPO_DIR"
ENV_FILE="$REPO_DIR/.env.voice"
BACKEND="passive-audio"
CHECK_ONLY=0
INSTALL_PACKAGES=1
INSTALL_UV=1
INSTALL_TOOL=1
SYSTEMD_USER=0
ENABLE_LINGER=0
FORCE_ENV=0
RUN_AUDIO_TEST=0
AUDIO_GROUP_ADDED=0

usage() {
  cat <<'USAGE'
Usage: scripts/install-client-linux.sh [options]

Installs Minigent's chat/voice client dependencies on a Linux host and can create a
systemd user service for always-on passive audio.

Options:
  --install-dir PATH      Repo/source directory used by the service. Default: repo root
  --env-file PATH         Client env file. Default: <repo>/.env.voice
  --backend BACKEND       Service backend: stdin, manual-audio, passive-audio. Default: passive-audio
  --systemd-user          Install and enable a systemd user service
  --enable-linger         Allow the systemd user service to run after logout
  --check-only            Print diagnostics without installing packages or services
  --skip-packages         Do not install OS packages
  --skip-uv-install       Do not install uv if it is missing
  --skip-tool-install     Do not run uv tool install
  --force-env             Overwrite an existing env file template
  --audio-test            Record and play a short ALSA test clip with the default device
  -h, --help              Show this help

Notes:
  If this script adds your user to the audio group, log out and back in before
  starting the client. Current sessions do not automatically gain new groups.
USAGE
}

log() {
  printf '[minigent-client-install] %s\n' "$*"
}

die() {
  printf '[minigent-client-install] ERROR: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

session_in_group() {
  id -nG | tr ' ' '\n' | grep -qx "$1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="${2:?missing value for --install-dir}"
      shift 2
      ;;
    --env-file)
      ENV_FILE="${2:?missing value for --env-file}"
      shift 2
      ;;
    --backend)
      BACKEND="${2:?missing value for --backend}"
      shift 2
      ;;
    --systemd-user)
      SYSTEMD_USER=1
      shift
      ;;
    --enable-linger)
      ENABLE_LINGER=1
      shift
      ;;
    --check-only)
      CHECK_ONLY=1
      INSTALL_PACKAGES=0
      INSTALL_UV=0
      INSTALL_TOOL=0
      SYSTEMD_USER=0
      ENABLE_LINGER=0
      RUN_AUDIO_TEST=0
      shift
      ;;
    --skip-packages)
      INSTALL_PACKAGES=0
      shift
      ;;
    --skip-uv-install)
      INSTALL_UV=0
      shift
      ;;
    --skip-tool-install)
      INSTALL_TOOL=0
      shift
      ;;
    --force-env)
      FORCE_ENV=1
      shift
      ;;
    --audio-test)
      RUN_AUDIO_TEST=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || die "this installer only supports Linux"
[[ -d "$INSTALL_DIR" ]] || die "install dir does not exist: $INSTALL_DIR"

case "$BACKEND" in
  stdin|manual-audio|passive-audio) ;;
  *) die "unsupported backend: $BACKEND" ;;
esac

if [[ "$INSTALL_PACKAGES" -eq 1 ]]; then
  if have_cmd apt-get; then
    log "Installing Debian/Ubuntu package prerequisites"
    sudo apt-get update
    sudo apt-get install -y git curl build-essential pkg-config portaudio19-dev libasound2-dev alsa-utils
  elif have_cmd dnf; then
    log "Installing Fedora/RHEL package prerequisites"
    sudo dnf install -y git curl gcc gcc-c++ make pkgconf-pkg-config portaudio-devel alsa-lib-devel alsa-utils
  elif have_cmd pacman; then
    log "Installing Arch package prerequisites"
    sudo pacman -S --needed git curl base-devel pkgconf portaudio alsa-lib alsa-utils
  else
    die "unsupported distro package manager; install git, curl, build tools, pkg-config, PortAudio, ALSA dev headers, and alsa-utils"
  fi
fi

if ! have_cmd uv; then
  if [[ "$INSTALL_UV" -eq 1 ]]; then
    log "Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else
    die "uv is not on PATH"
  fi
fi

if [[ "$INSTALL_TOOL" -eq 1 ]]; then
  log "Installing minigent-client with the voice extra"
  (cd "$INSTALL_DIR" && uv tool install --reinstall --editable '.[voice]')
fi

log "Audio diagnostics"
if [[ -r /proc/asound/cards ]]; then
  cat /proc/asound/cards
else
  log "/proc/asound/cards is not readable"
fi

if [[ -d /dev/snd ]]; then
  ls -l /dev/snd
else
  log "/dev/snd does not exist"
fi

if have_cmd arecord; then
  arecord -l || true
else
  log "arecord is not installed"
fi

if have_cmd aplay; then
  aplay -l || true
else
  log "aplay is not installed"
fi

if getent group audio >/dev/null 2>&1; then
  if ! session_in_group audio; then
    if [[ "$CHECK_ONLY" -eq 1 ]]; then
      log "Current session is not in the audio group"
    else
      log "Adding $USER to the audio group"
      sudo usermod -aG audio "$USER"
      AUDIO_GROUP_ADDED=1
      log "Log out and back in before starting the client so this session gains audio access"
    fi
  else
    log "Current session is already in the audio group"
  fi
else
  log "No audio group exists on this host; relying on existing /dev/snd permissions"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  log "Check-only mode complete"
  exit 0
fi

if [[ ! -f "$ENV_FILE" || "$FORCE_ENV" -eq 1 ]]; then
  log "Writing env template to $ENV_FILE"
  mkdir -p "$(dirname "$ENV_FILE")"
  umask 077
  cat > "$ENV_FILE" <<'EOF'
MINIGENT_BASE_URL=http://127.0.0.1:8000
MINIGENT_VOICE_API_TOKEN=

MINIGENT_VOICE_STT_PROVIDER=openai
OPENAI_API_KEY=

MINIGENT_VOICE_TTS_PROVIDER=piper
MINIGENT_VOICE_TTS_MODEL=en_US-lessac-medium

MINIGENT_VOICE_WAKE_ACKNOWLEDGEMENT=bell
# MINIGENT_VOICE_CAPTURE_ENDED_ACKNOWLEDGEMENT=bell
MINIGENT_VOICE_WAKEWORD_PROVIDER=openwakeword
MINIGENT_VOICE_OWW_MODEL=okay_nabu
MINIGENT_VOICE_FOLLOW_UP_TIMEOUT_MS=6000
EOF
else
  log "Keeping existing env file: $ENV_FILE"
fi

if [[ "$RUN_AUDIO_TEST" -eq 1 ]]; then
  have_cmd arecord || die "arecord is required for --audio-test"
  have_cmd aplay || die "aplay is required for --audio-test"
  log "Recording five seconds from the default ALSA input"
  arecord -D default -f cd -d 5 /tmp/minigent-audio-test.wav
  log "Playing /tmp/minigent-audio-test.wav through the default ALSA output"
  aplay /tmp/minigent-audio-test.wav
fi

if [[ "$SYSTEMD_USER" -eq 1 ]]; then
  SERVICE_DIR="$HOME/.config/systemd/user"
  SERVICE_PATH="$SERVICE_DIR/minigent-client.service"
  LEGACY_DAEMON_SERVICE_PATH="$SERVICE_DIR/minigent-daemon.service"
  LEGACY_VOICE_DAEMON_SERVICE_PATH="$SERVICE_DIR/minigent-voice-daemon.service"
  RUNNER="$INSTALL_DIR/scripts/run-client-linux.sh"

  [[ -x "$RUNNER" ]] || die "runner is not executable: $RUNNER"
  mkdir -p "$SERVICE_DIR"

  if [[ -f "$LEGACY_DAEMON_SERVICE_PATH" ]]; then
    log "Removing legacy minigent-daemon user service"
    systemctl --user disable --now minigent-daemon >/dev/null 2>&1 || true
    rm -f "$LEGACY_DAEMON_SERVICE_PATH"
  fi

  if [[ -f "$LEGACY_VOICE_DAEMON_SERVICE_PATH" ]]; then
    log "Removing legacy minigent-voice-daemon user service"
    systemctl --user disable --now minigent-voice-daemon >/dev/null 2>&1 || true
    rm -f "$LEGACY_VOICE_DAEMON_SERVICE_PATH"
  fi

  log "Writing systemd user service to $SERVICE_PATH"
  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Minigent chat/voice client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=MINIGENT_VOICE_ENV_FILE=$ENV_FILE
Environment=MINIGENT_VOICE_BACKEND=$BACKEND
ExecStart=$RUNNER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable minigent-client

  if [[ "$ENABLE_LINGER" -eq 1 ]]; then
    log "Enabling linger for $USER"
    sudo loginctl enable-linger "$USER"
  fi

  if [[ "$AUDIO_GROUP_ADDED" -eq 1 ]] || { getent group audio >/dev/null 2>&1 && ! session_in_group audio; }; then
    log "Service installed but not started because this session is not yet in the audio group"
    log "Log out and back in, then run: systemctl --user start minigent-client"
  else
    log "Starting systemd user service"
    systemctl --user restart minigent-client
  fi
fi

log "Done"
log "Edit env file: $ENV_FILE"
log "Manual smoke test: cd $INSTALL_DIR && MINIGENT_VOICE_ENV_FILE=$ENV_FILE scripts/run-client-linux.sh --backend stdin"
log "Audio smoke test: cd $INSTALL_DIR && MINIGENT_VOICE_ENV_FILE=$ENV_FILE scripts/run-client-linux.sh --backend manual-audio --once"
if [[ "$SYSTEMD_USER" -eq 1 ]]; then
  log "Service logs: journalctl --user -u minigent-client -f"
fi
