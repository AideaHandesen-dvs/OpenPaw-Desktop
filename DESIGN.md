# OpenPaw Desktop 設計書 v2

> **運用メモ（Claudeへ）**
> このドキュメントはセッション間の唯一の真実（single source of truth）です。
> 新しいセッションを開始する際は、このファイルを最初に共有してください。
> 実装・修正・追加決定があれば、必ずこのドキュメントを更新してから次のセッションに進んでください。

---

## 1. プロジェクト概要

OpenPaw Desktopは、Linuxデスクトップ環境を安全かつ自動的に操作するローカルAIエージェントである。
ブラウザ自動化機能を除外し、CLI・ファイル操作・D-Bus・GUI（最終手段）に集中する。

---

## 2. 確定済み技術スタック

| 項目 | 決定内容 |
|------|---------|
| 実装言語 | Python |
| LLM | qwen2.5:7b（Ollama経由、ローカル完結） |
| UI | CLI 1回実行（セッション管理なし） |
| ターゲット環境 | Debian + KDE Plasma |
| Ollamaの出力形式 | `format: json`（structured output）を使用 |
| Ollama接続先 | 環境変数 `OLLAMA_URL`（デフォルト: localhost:11434） |
| LLMモデル   | 環境変数 OLLAMA_MODEL（デフォルト: qwen2.5:7b）   |

---

## 3. 設計目標

- ローカル完結動作（外部API不使用）
- 安全な自動実行
- 予測可能な（deterministic）処理
- 監査可能な操作ログ
- GUI依存を最小化

---

## 4. アーキテクチャ

```
$ openpaw "Downloadsの古いPDFをDocumentsに移動して"
    ↓
Task Planner（qwen2.5:7b via Ollama）
    ↓ JSON Plan生成（format: json）
Safety Checker
    ├─ danger_levelを判定
    ├─ allowlistチェック
    └─ パス制限チェック
    ↓
HITL（danger_level >= 1 の場合）
    └─ ユーザーに確認 → y/n
    ↓
Tool Executor（順次実行、失敗時即中断）
    ├─ shell
    ├─ filesystem
    ├─ dbus（Phase 2）
    └─ gui（Phase 3、保証なし）
    ↓
Audit Logger
    ↓
結果表示して終了
```

---

## 5. JSONプランスキーマ（正式定義）

LLMが生成するJSONの構造。Ollamaの`format: json`で強制する。

```json
{
  "task_summary": "タスクの一行説明",
  "steps": [
    {
      "step_id": 1,
      "tool": "shell",
      "description": "このステップが何をするかの説明",
      "command": "ls -la ~/Downloads",
      "danger_level": 0,
      "on_error": "abort"
    },
    {
      "step_id": 2,
      "tool": "filesystem",
      "description": "PDFをDocumentsへ移動",
      "action": "move",
      "src": "~/Downloads/*.pdf",
      "dst": "~/Documents/",
      "danger_level": 1,
      "on_error": "abort"
    },
    {
      "step_id": 3,
      "tool": "dbus",
      "description": "KRunnerの検索履歴をクリア",
      "action": "call",
      "service": "org.kde.krunner",
      "object": "/App",
      "interface": "org.kde.krunner.App",
      "method": "CleanHistory",
      "args": [],
      "arg_types": [],
      "bus": "session",
      "danger_level": 1,
      "on_error": "abort"
    }
  ]
}
```

### フィールド定義

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `task_summary` | string | ✅ | タスク全体の一行説明 |
| `steps` | array | ✅ | 実行ステップのリスト |
| `step_id` | int | ✅ | ステップ番号（1始まり） |
| `tool` | enum | ✅ | `shell` / `filesystem` / `dbus` / `gui` |
| `description` | string | ✅ | ステップの人間向け説明 |
| `command` | string | shellのみ必須 | 実行するbashコマンド |
| `src` | string | filesystemのみ必須 | 操作元パス |
| `dst` | string | copy/moveのみ必須 | 操作先パス |
| `danger_level` | int | ✅ | 0〜3（下記参照） |
| `on_error` | enum | ✅ | 現在は`abort`のみ |
| `action` | string | filesystem/dbusのみ必須 | filesystem: `copy` / `move` / `delete` / `mkdir`<br>dbus: `call` / `get` / `set` / `list` / `introspect` |
| `service`   | string | dbusのみ必須      | D-Busバス名 e.g. `org.kde.krunner` |
| `object`    | string | dbusのみ必須      | オブジェクトパス e.g. `/App` |
| `interface` | string | dbusのみ必須      | インターフェース名 e.g. `org.kde.krunner.App` |
| `method`    | string | `call`のみ必須    | 呼び出すメソッド名 |
| `property`  | string | `get`/`set`のみ必須 | 対象プロパティ名 |
| `args`      | array  | 任意              | メソッド/プロパティへの引数リスト |
| `arg_types` | array  | 任意              | busctl型シグネチャ e.g. `["s","i"]`。省略時はintrospectで自動取得。複合型（`a`系）は未対応 |
| `bus`       | string | 任意              | `session`（デフォルト）または `system` |

#### gui ツール固有フィールド

| フィールド  | 型     | 必須条件                        | 説明 |
|------------|--------|-------------------------------|------|
| `action`   | enum   | ✅                             | `key` / `type` / `click` / `move` / `scroll` / `focus` / `screenshot` / `getwindows` |
| `keys`     | string | `key`のみ必須                  | xdotool形式のキー名 e.g. `"ctrl+c"`, `"Return"`, `"super+d"` |
| `text`     | string | `type`のみ必須                 | 入力するテキスト |
| `delay`    | int    | 任意                           | `type` の文字間ディレイ（ms、デフォルト12） |
| `x`        | int    | `click`/`move`のみ必須         | 画面X座標（絶対座標） |
| `y`        | int    | `click`/`move`のみ必須         | 画面Y座標（絶対座標） |
| `button`   | int    | 任意                           | クリックボタン 1=左 2=中 3=右（デフォルト1） |
| `count`    | int    | 任意                           | クリック回数（デフォルト1、ダブルクリックは2） |
| `direction`| string | `scroll`のみ必須               | `up` / `down` / `left` / `right` |
| `amount`   | int    | 任意                           | スクロール量（ステップ数、デフォルト3） |
| `target`   | string | `focus`のみ必須                | ウィンドウタイトル（部分一致） |
| `path`     | string | 任意                           | `screenshot` の保存先（省略時は `~/.openpaw/screenshots/<timestamp>.png`） |

#### gui バックエンド優先順位

| 機能                         | 第1候補（Wayland） | 第2候補（X11）    |
|-----------------------------|--------------------|------------------|
| key / type / click / move / scroll | ydotool      | xdotool          |
| focus / getwindows          | wmctrl             | xdotool          |
| screenshot                  | scrot              | spectacle / import |

> **注意**: ydotool は `ydotoold` デーモンの起動が必要。xdotool は X11 セッション専用。

---

## 6. 危険度レベル定義

| Level | 分類 | 挙動 | 該当操作例 |
|-------|------|------|-----------|
| 0 | 読み取り専用 | 自動実行 | `ls`, `cat`, `find` |
| 1 | ファイル変更 | HITL確認（y/n） | `mv`, `cp`, `mkdir` |
| 2 | 削除・権限変更 | HITL確認 + 警告表示 | `rm`, `chmod`, `chown`, `kill` |
| 3 | ブロック | 実行不可・即中断 | `rm -rf /`, `sudo`, `dd`, `mkfs` |

---

## 7. Safety Checker

### allowlist / blocklist（設定ファイル: `config/safety.yaml`）

```yaml
# 実行を完全に禁止するパターン
blocklist:
  - "rm -rf /"
  - "rm -rf ~"
  - "sudo"
  - "su "
  - "dd "
  - "mkfs"
  - "> /dev/"
  - "chmod 777 /"

# danger_level: 2 として扱うコマンドprefixまたはパターン
dangerous:
  - "rm"
  - "mv"
  - "chmod"
  - "chown"
  - "kill"
  - "killall"
  - "systemctl stop"
  - "systemctl disable"

# 操作を許可するパスプレフィックス（これ以外は拒否）
allowed_paths:
  - "/home/john"
  - "/tmp"

# 実行タイムアウト（秒）
timeout: 30
```

### チェック順序

1. blocklistに一致 → Level 3、即中断
2. パス制限チェック → allowed_paths外なら中断
3. dangerousパターン → Level 2
4. filesystemのdelete操作 → Level 2
5. filesystemのmove/copy操作 → Level 1
6. それ以外 → Level 0

---

## 8. HITL（Human-in-the-Loop）

Level 1以上のステップは実行前にユーザー確認を求める。

```
[OpenPaw] タスク: PDFをDocumentsへ移動

ステップ 1/2: ファイル一覧確認
  ツール: shell
  コマンド: ls -la ~/Downloads
  危険度: 0（自動実行）

ステップ 2/2: ファイルを移動
  ツール: filesystem
  操作: move ~/Downloads/*.pdf → ~/Documents/
  危険度: 1 ⚠️  確認が必要です

実行しますか？ [y/N]:
```

- デフォルトは`N`（Enterだけで中断）
- `--yes`フラグで全確認をスキップ（上級者向け）

---

## 9. 失敗時の挙動

- **即中断（abort）**：あるステップが失敗したら、後続ステップは実行しない
- 失敗した時点でAudit Logに記録して終了
- ロールバックは行わない（MVPスコープ外）

---

## 10. 監査ログ

保存先: `~/.openpaw/logs/YYYY-MM-DD.jsonl`（1行1エントリ）

```json
{
  "timestamp": "2026-05-17T21:00:00+09:00",
  "task_summary": "PDFをDocumentsへ移動",
  "step_id": 2,
  "tool": "filesystem",
  "action": "move",
  "src": "~/Downloads/*.pdf",
  "dst": "~/Documents/",
  "danger_level": 1,
  "user_confirmed": true,
  "status": "success",
  "output": "3 files moved"
}
```

---

## 11. ディレクトリ構成（予定）

```
openpaw/
├─ main.py               # エントリポイント（CLI引数受付）
├─ planner.py            # Task Planner（Ollama呼び出し）
├─ safety.py             # Safety Checker
├─ logger.py             # Audit Logger
├─ tools/
│   ├─ shell.py          # shellツール実装
│   ├─ filesystem.py     # filesystemツール実装
│   ├─ dbus.py           # dbusツール実装（Phase 2）
│   └─ gui.py            # guiツール実装（Phase 3）
├─ config/
│   └─ safety.yaml       # Safety Checker設定
└─ tests/
    ├─ test_safety.py
    ├─ test_executor.py
    └─ test_planner.py
```

---

## 12. MVPフェーズ

### Phase 1（最初に動かすもの）
- [x] `main.py`: CLI引数受付、全体フロー制御
- [x] `planner.py`: Ollama呼び出し、JSONプラン生成
- [x] `safety.py`: danger_level判定、blocklist/allowlistチェック
- [x] `tools/shell.py`: bashコマンド実行
- [x] `tools/filesystem.py`: cp/mv/rm/mkdir
- [x] `logger.py`: jsonl形式でログ保存
- [x] HITLの確認プロンプト

### Phase 2
- [x] `tools/dbus.py`: qdbus/busctl統合（auto-introspect, introspect アクション含む）

### Phase 3
- [x] `tools/gui.py`: ydotool / AT-SPI（KDE/Debian限定、保証なし）

---

## 13. 非採用機能（現フェーズ）

- ブラウザ自動化、Playwright連携
- セッション管理・会話履歴
- ロールバック機能
- マルチDE対応（GNOMEなど）
- Windows / macOS対応

---

## 14. 開発の段取り（次セッション以降）

### Step 1: 環境セットアップ確認
新セッション開始時にClaudeへ伝えること：
```
この設計書（DESIGN.md）を読んで現状を把握してください。
```

### Step 2: 実機テスト環境（2026-05-18 構築済み）

| 項目 | 内容 |
|------|------|
| 開発機 | gandalfr（Debian 12、ヘッドレス） |
| テスト用VM | openpaw-kde（KVM、Debian 12 + KDE Plasma） |
| VMへのSSH | `ssh openpaw-vm`（gandalfr上、パスワードなし） |
| VMへのGUI接続 | fenrirから `ssh -f -N -L 5902:localhost:5900 john@gandalfr` → Remmina `spice://localhost:5902` |
| Ollama | durandal:11434（gandalfr上でポートフォワード済み） |

**VM起動手順（gandalfr上）:**
```bash
virsh start openpaw-kde
# Ollama フォワード（未起動の場合）
ssh -f -N -L 0.0.0.0:11434:durandal:11434 john@durandal
```

**VM内の実行環境（openpaw-vm上）:**
```bash
cd ~/openpaw-desktop
source .venv-desktop/bin/activate
export DISPLAY=:1
export XAUTHORITY=/run/user/1000/xauth_zLUXKt  # 毎回変わる。`echo $XAUTHORITY` で確認
export OLLAMA_URL=http://10.0.2.2:11434
```

### Step 3: 動作確認コマンド例
```bash
# dry-run（実行せず計画だけ表示）
python main.py --dry-run "Downloadsの古いPDFをDocumentsに移動して"

# 通常実行
python main.py "Downloadsの古いPDFをDocumentsに移動して"

# 確認スキップ（上級者向け）
python main.py --yes "tmpフォルダを空にして"

# 動作確認済みのコマンド
python main.py "スクリーンショットを撮って"
# → ~/.openpaw/screenshots/<timestamp>.png に保存される
```

### Step 4: 既知の問題・注意事項

- `XAUTHORITY` のパスはログインのたびに変わる。`echo $XAUTHORITY` で確認してから設定すること
- qwen2.5:7b は gui ツールの `action` フィールドを `command` に詰め込む癖がある
  → `planner.py` の `_normalize_gui_step()` で救済済み
- ydotool 0.1.8 に `scroll` サブコマンドは存在しない → xdotool にフォールバック済み
- ydotool 0.1.8 に `ydotoold.service` は存在しない。`/dev/uinput` への権限があれば動く
  → `john` を `input` グループに追加済み（VM内）

### Step 5: 各セッションの終わりに
- 実装した内容を設計書の該当チェックボックスにチェックを入れる
- 設計変更があれば設計書を更新してから終了する
