#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_venv="$(mktemp -d -t mailarchive-build-venv.XXXXXX)"
app_dir="$project_root/build/MailArchive.AppDir"
appimage_tool="${APPIMAGETOOL:-$(command -v appimagetool || true)}"

cleanup() {
  rm -rf -- "$build_venv"
}
trap cleanup EXIT

python3 -m venv "$build_venv"
build_python="$build_venv/bin/python"
"$build_python" -m pip install --upgrade pip
"$build_python" -m pip install -e "$project_root" "pyinstaller>=6,<7"

cd "$project_root"
"$build_python" -m mailarchive.provider_config
"$build_python" -m unittest discover -s tests -v
mkdir -p "$project_root/build/spec"
"$build_python" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --windowed \
  --name MailArchive \
  --specpath "$project_root/build/spec" \
  --paths "$project_root/src" \
  --collect-all google_auth_oauthlib \
  --collect-all msal \
  --collect-submodules google.auth \
  --collect-submodules google.oauth2 \
  --collect-all keyring \
  --collect-all secretstorage \
  --collect-all dbus_next \
  "$project_root/src/mailarchive/__main__.py"

"$project_root/dist/MailArchive/MailArchive" --smoke-test
cp "$project_root/LICENSE" "$project_root/dist/MailArchive/LICENSE"

rm -rf "$app_dir"
mkdir -p "$app_dir/usr/lib/mailarchive" "$app_dir/usr/share/metainfo"
cp -a "$project_root/dist/MailArchive/." "$app_dir/usr/lib/mailarchive/"
cp "$project_root/packaging/linux/AppRun" "$app_dir/AppRun"
cp "$project_root/packaging/linux/mailarchive.desktop" "$app_dir/mailarchive.desktop"
cp "$project_root/packaging/linux/mailarchive.appdata.xml" "$app_dir/usr/share/metainfo/mailarchive.appdata.xml"
cp "$project_root/assets/mailarchive.svg" "$app_dir/mailarchive.svg"
chmod +x "$app_dir/AppRun"
ln -sfn mailarchive.svg "$app_dir/.DirIcon"

if [[ -z "$appimage_tool" ]]; then
  echo "AppDir created: $app_dir"
  echo "Install appimagetool or set APPIMAGETOOL=/path/to/appimagetool to create the AppImage."
  exit 2
fi

version="$($build_python -c 'import mailarchive; print(mailarchive.__version__)')"
architecture="$(uname -m)"
ARCH="$architecture" "$appimage_tool" \
  "$app_dir" \
  "$project_root/dist/MailArchive-$version-$architecture.AppImage"

echo "Done: $project_root/dist/MailArchive-$version-$architecture.AppImage"
