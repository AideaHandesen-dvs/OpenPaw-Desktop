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
      "description": "KRunnerを開閉する",
      "action": "call",
      "service": "org.kde.krunner",
      "object": "/App",
      "interface": "org.kde.krunner.App",
      "method": "toggleDisplay",
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

### 手動テスト結果（2026-05-19実施 2026-05-20追記）

| ID | コマンド | 結果 | 備考 |
|----|---------|------|------|
| T-01 | `"Downloadsのファイル一覧を見せて"` | ✅ | shell/ls 正常動作 |
| T-02 | `"ホームディレクトリに test.txt を作って"` | ✅ | shell/touch 正常動作 |
| T-03 | `"test.txt を Documents に移動して"` | ✅ | 当初はshell mv（BUG-005）→修正後はfilesystemツール選択 |
| T-04 | `"スクリーンショットを撮って"` | ✅ | gui/screenshot 正常動作 |
| T-05 | `"KRunnerを開いて"` | ✅ | dbus/toggleDisplay 動作（開閉なので実質OK） |
| T-06 | `--dry-run "KRunnerを開閉して"` | ✅ | introspect→dry-run 正常動作 |
| T-07 | `"KRunnerの履歴を消して"` | ❌ | toggleDisplay×2を実行（BUG-006/007参照） |
| T-08 | `--yes "Documentsの古いpdfを削除してくれ．"` | ✅ | action:delete生成・Level2確認必須・$prev複数件正常動作 |
| T-10 | `"/etc/passwdを見せて"` | ✅ | shell パス制限が実機で動作、danger_level:3 でブロック確認 |

### Step 4: 既知の問題・注意事項

- `XAUTHORITY` のパスはログインのたびに変わる。`echo $XAUTHORITY` で確認してから設定すること
- qwen2.5:7b は gui ツールの `action` フィールドを `command` に詰め込む癖がある
  → `planner.py` の `_normalize_gui_step()` で救済済み
- ydotool 0.1.8 に `scroll` サブコマンドは存在しない → xdotool にフォールバック済み
- ydotool 0.1.8 に `ydotoold.service` は存在しない。`/dev/uinput` への権限があれば動く
  → `john` を `input` グループに追加済み（VM内）
- ~~`action:rm` 等の不正なfilesystemアクションがLevel0で素通りする~~ → safety.pyで許可リスト外をLevel3ブロックに修正済み
- ~~`$prev`が空（findが0件）のとき`src:""`→`delete(".")`を試みクラッシュ~~ → 空の場合はスキップして正常終了するよう修正済み
- ~~shell コマンド内のパスが allowed_paths 外でも素通りする~~ → safety.pyの`_extract_paths_from_command()`で修正済み（Section 17参照）

### Step 5: 各セッションの終わりに
- 実装した内容を設計書の該当チェックボックスにチェックを入れる
- 設計変更があれば設計書を更新してから終了する

### 次セッションの作業計画（2026-05-20確定）

1. ~~**候補A: shell パス制限の実機確認**~~ ✅ 完了（2026-05-20、T-10参照）

2. **候補B: end-to-end テストの整備**
   - 現状は各モジュールの単体テストのみ
   - main.py を通した統合テスト（dry-run ベース）を追加

3. **候補C: `~/` パスの config.yaml 対応確認**
   - safety.yaml の `allowed_paths` に `~/` 記法が使えるか整理
   - 現状は `${HOME}` 展開のみ対応、`~/` は `_is_allowed_path()` 内で `expanduser()` が処理

---

## 17. shell ツールのパス制限

### 背景

`filesystem` ツールは `action` 許可リストで守れているが、`shell` ツールは
任意の bash コマンドを実行できるため、コマンド文字列内のパスが
`allowed_paths` の外を指しても素通りしていた（例: `cat /etc/passwd`）。

### 設計方針（ベストエフォート）

シェルコマンドの完全パースは困難なため「できる範囲でやる」設計とする。

**抽出対象:**
- `/` で始まる絶対パス
- `~/` で始まるホーム相対パス

**抽出非対象（既知の限界）:**
- 相対パス（`./foo`, `../bar`）: cwd が不明なため
- スペースを含むクォートパス: 簡易実装のため先頭部分しか取れない

**処理手順（`_extract_paths_from_command`）:**
1. ダブル/シングルクォートを除去（簡易）
2. `|><&;` をスペースに変換（リダイレクト・パイプ内のパスも対象にする）
3. トークン分割し、`-` 始まり（フラグ）をスキップ
4. `/` または `~/` で始まるトークンをパス候補として収集

**チェック位置:** `check()` のステップ2（パス制限チェック）で
`filesystem` の `src`/`dst` チェックと同じ `_is_allowed_path()` を再利用。

### 実装場所

- `safety.py`: `_extract_paths_from_command()` を追加、`check()` のステップ2を拡張
- `tests/test_safety.py`:
  - `TestShellPathRestriction`（統合テスト 12ケース）
  - `TestExtractPathsFromCommand`（単体テスト 8ケース）
  - `if __name__ == "__main__"` をファイル末尾に移動（既存バグ修正）

### 背景

現状のexecutorは各ステップを独立して実行するだけで、前のステップの出力を
次のステップが使えない（BUG-001）。これにより「古いファイルだけ移動」のような
条件付き操作が意図通りに動かない。

### 設計方針

**案Bを採用: Pythonのexecutorが引き渡しを管理する**

LLMにプレースホルダーを書かせる案（案A）は、7bモデルがルールを守らない
可能性が高くBUG-006と同根の問題を引き起こすため採用しない。
「LLMを信用しすぎない、コードで強制する」という本プロジェクトの方針に従い、
Pythonコードが引き渡しの全責務を持つ。

**フェーズ分割: まずB-1（シンプル版）から実装する**

| 案 | できること | 実装コスト |
|----|-----------|-----------|
| B-1: 前ステップのoutputをそのまま次のsrcに渡す | 典型的なユースケースをカバー | 低 |
| B-2: outputをパースして構造化データとして渡す | より複雑な操作に対応 | 高 |

B-1で典型ユースケース（find結果→移動等）をカバーし、必要に応じてB-2に拡張する。

### B-1の設計詳細

#### 対象パターン

```
Step1: shell → 1行1ファイルのパスリストを出力
Step2: filesystem/move または filesystem/copy → srcにStep1の出力を使いたい
```

#### LLMへの指示（SYSTEM_PROMPT追加）

Step1のshellステップに `"capture_output": true` フラグを立てさせる。
Step2のfilesystemステップの `src` に `"{steps[1].output}"` ではなく
`"$prev"` のような固定キーワードを書かせる。

```json
{"step_id": 1, "tool": "shell", "command": "find ~/Downloads -name '*.pdf' -mtime +30",
 "capture_output": true, "danger_level": 0, "on_error": "abort"}
{"step_id": 2, "tool": "filesystem", "action": "move",
 "src": "$prev", "dst": "~/Documents/", "danger_level": 1, "on_error": "abort"}
```

`$prev` という固定キーワードにすることでLLMが覚えやすく、
executorが検出しやすい。

#### executorの変更（main.py）

```python
prev_output: str | None = None  # 直前ステップの出力

for step in plan.steps:
    # $prev を実際の出力で置換
    if step.get("src") == "$prev":
        if prev_output is None:
            # エラー: 前ステップに出力がない → return 1
            ...
        lines = [l for l in prev_output.strip().splitlines() if l.strip()]
        if len(lines) > 1:
            # 複数ファイル → 1件ずつ execute_step を呼んで continue
            for path in lines:
                sub_step = {**step, "src": path}
                execute_step(sub_step)  # ログ・エラー処理含む
            prev_output = None
            continue
        else:
            step["src"] = lines[0] if lines else ""

    success, output, error = execute_step(step)

    # capture_output フラグがあれば出力を保持
    if step.get("capture_output") and success:
        prev_output = output
    else:
        prev_output = None  # フラグがなければリセット
```

#### 影響範囲

- `main.py`: executorループの修正
- `planner.py`: SYSTEM_PROMPTに`$prev`と`capture_output`の例を追加
- `tests/test_prev_handoff.py`: executor引き渡しテスト（10ケース）追加
- `safety.py`: `paths_to_check` から `"$prev"` を除外（BUG-008対応）

#### 未対応（スコープ外）

- 2ステップ以上前の出力を参照する（`$steps[1].output`のような多段参照）
- 条件分岐（Step1の結果によってStep2の内容を変える）
- shell以外のツール（filesystem等）のoutputを引き渡しに使う

### ~~[BUG-001] ステップ間のデータ引き渡しが機能しない~~ ✅ 解決済み

**発見日:** 2026-05-18  
**解決日:** 2026-05-19  
**再現手順:** `python main.py "Downloadsの古いPDFをDocumentsに移動して"`  
**症状:** LLMが `find -mtime +7` で古いファイルを絞り込むステップを生成しても、
次のfilesystemステップのsrcはハードコードされた `~/Downloads/*.pdf` のままで
全件移動される。  
**原因:** ステップ間でデータを引き渡す仕組みがない。各ステップは独立実行のみ。  
**影響:** 条件付き操作（「古いファイルだけ」「サイズが大きいものだけ」等）が
すべて意図通りに動かない。  
**対処:** Section 16 B-1 を実装（`capture_output` + `$prev`）。  
複数ファイル返却時は1件ずつ `execute_step` を呼ぶことで対処（BUG-008も同時解決）。  
**確認:** `python main.py "古いPDFだけDocumentsに移動して"` →
`old1.pdf` / `old2.pdf` のみ移動、`new.pdf` は残留。


### ~~[BUG-003] SYSTEM_PROMPTのdbus例のメソッドが実在しない~~ ✅ 解決済み

**発見日:** 2026-05-19  
**解決日:** 2026-05-19  
**原因:** DESIGN.md および SYSTEM_PROMPT の例として使用した
org.kde.krunner.App.CleanHistory がこの環境のKRunnerに存在しなかった。  
**対処:** SYSTEM_PROMPT の dbus 例を `toggleDisplay`（KRunner開閉）に差し替え。
`task_summary` / `description` も実際の動作に合わせて修正。`arg_types: []` を例に追記。
DESIGN.md Section 5 の JSON 例も同様に更新。

### ~~[BUG-005] 単純な移動タスクでshell mvが選ばれfilesystemツールが使われない~~ ✅ 解決済み

**発見日:** 2026-05-19
**解決日:** 2026-05-19
**原因:** SYSTEM_PROMPTにfilesystemツールのmove例が不足しており、LLMがshell mvを選択。
danger_levelが1ではなく2になっていた。
**対処:** SYSTEM_PROMPTに単純移動の例（test.txt→Documents）を追加し、
「ファイルのコピー・移動・削除はfilesystemツールを使う」ルールを明記。
**確認:** `python main.py "test2.txt を Documents に移動して"` →
filesystemツール/danger_level 1 で成功。


### [BUG-006] dbusメソッド検証: 存在しないメソッドの強行 → 🔶 部分解決

**発見日:** 2026-05-19
**実装日:** 2026-05-19
**対処内容:**
- `_parse_method_names()`: introspect結果からメソッド名のsetを抽出
- `_enforce_dbus_methods()`: dbus/callステップのmethodがsetに存在しなければ
  shellフォールバックで再プランニング（コードレベル強制）
- `plan()` に検証フローを組み込み

**残存する問題（スコープ外）:**
「存在するが意味的に間違ったメソッド」は防げない。
「KRunnerの履歴を消して」に対してLLMが `toggleDisplay`（実在メソッド）を
2回呼ぶプランを生成した場合、コードはそれを正しく通過させてしまう。
これはqwen2.5:7bの意味理解の限界であり、コードレベルでの検出は不可能。


### [BUG-007] KRunner履歴クリアがD-Busで達成できない → 対象外（won't fix）

**発見日:** 2026-05-19  
**調査完了:** 2026-05-19  
**調査結果:**
- 履歴の実体は `~/.local/share/krunnerstaterc` の `[PlasmaRunnerManager][History]`
- `~/.config/krunnerrc` には履歴は含まれない
- KRunner起動中はファイルをメモリにキャッシュしており、直接編集しても終了時に上書きされる
- 正しい手順は「KRunner停止 → ファイル編集 → KRunner再起動」の3ステップ

**対処方針:** 対象外。複雑さに対してユースケースが薄い。
このタスクをユーザーが入力した場合の挙動は現状のまま（誤ったdbusを実行）でも、
そもそもこのタスクをエージェントに投げることを推奨しないという運用方針とする。
将来的にはSYSTEM_PROMPTに「KRunner履歴クリアはサポート対象外」と明記することも検討。


### ~~[BUG-008] SafetyCheckerが`$prev`をリテラルパスとして拒否する~~ ✅ 解決済み

**発見日:** 2026-05-19  
**解決日:** 2026-05-19  
**再現手順:** Section 16 B-1 実装後に `python main.py "古いPDFだけDocumentsに移動して"` を実行  
**症状:** ステップ2（`src: "$prev"`）が `danger_level: 3` でブロックされ実行されない。  
エラー: `Path not in allowed_paths: '$prev'`  
**原因:** SafetyCheckerのパス制限チェックがexecutorによる`$prev`置換より前に走るため、
`"$prev"` という文字列がそのままパスとして評価される。  
**対処:** `safety.py` の `paths_to_check` から `"$prev"` を除外。  
実際のパス検証はexecutor実行時にfilesystemツール側で行われる。  
```python
paths_to_check = [p for p in (src, dst) if p and p != "$prev"]
```
