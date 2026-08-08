# SaveSmith as a Homebrew cask.
#
# The point of this file is one flag: `--no-quarantine`. SaveSmith is signed
# ad-hoc and not notarised — notarisation needs Apple's paid membership — and
# on Apple Silicon a quarantined app in that state does not get the polite
# "unidentified developer" dialog with an "Open Anyway" button. It gets
# "SaveSmith is damaged and can't be opened", with no way forward at all, which
# reads to anybody as "this download is broken or malicious".
#
# Installed through Homebrew with --no-quarantine, the browser never marks the
# file, so none of that happens and the app opens like any other.
#
# This lives in the project's own repository rather than a separate
# homebrew-savesmith one, because `brew tap` takes a URL and a second repo would
# be one more thing to keep in step for no gain. The release job rewrites the
# version and the checksums here on every tag, so a cask cannot quietly go
# stale and start installing last month's build.
cask "savesmith" do
  version "0.1.0"

  arch arm: "arm64", intel: "x64"

  sha256 arm:   "4ef733e73ed6270243e3aedbf4b15c57b0470d28af129136cfe443777bdb78c9",
         intel: "46b8e6d8c65efe5f1971812d3a8ba129492ca7514c9b7dc30483fb85052613ee"

  # No `verified:` here: it exists to vouch for a download host that differs
  # from the homepage, and both of these are github.com.
  url "https://github.com/Daxil/SaveSmith/releases/download/v#{version}/SaveSmith-#{version}-macos-#{arch}.dmg"

  name "SaveSmith"
  desc "Save editor for single-player games that works out unknown formats"
  homepage "https://github.com/Daxil/SaveSmith"

  livecheck do
    url :url
    strategy :github_latest
  end

  # The app updates itself once installed, so Homebrew should not fight it.
  auto_updates true
  depends_on macos: ">= :ventura"

  app "SaveSmith.app"

  caveats <<~CAVEATS
    Устанавливай с --no-quarantine, иначе macOS скажет «SaveSmith is damaged»:

      brew install --cask --no-quarantine savesmith

    Так выглядит отсутствие подписи Apple, а не испорченный файл: заверение
    стоит $99 в год, которых у проекта пока нет. Если уже установил без флага:

      xattr -dr com.apple.quarantine "#{appdir}/SaveSmith.app"
  CAVEATS

  # Copies of save files live here, and they are the whole point of the
  # program's promise. They go only on an explicit `brew uninstall --zap`.
  zap trash: [
    "~/Library/Application Support/SaveSmith",
    "~/Library/Caches/com.savesmith.desktop",
    "~/Library/Preferences/com.savesmith.desktop.plist",
    "~/Library/Saved Application State/com.savesmith.desktop.savedState",
  ]
end
