# 競技契約テンプレート

`*.json` は、モデルやLLMから独立した機械可読契約のプロファイルです。`template.json`を新競技の出発点にし、既存4競技のJSONは履歴を再現する参照プロファイルとして扱います。

- `sad.json`: 時間ブロックCV、Test-like mask、`ID,Target`、RMSE。
- `geoai.json`: Train-only nested/domain CV、`ID,TargetF1,TargetRAUC`、`0.6*F1+0.4*AUC`。
- `nedo.json`: exact replay、公式ゲート、`base/agent.py`入りzip。proxyは公式LBの代替にしない。
- `rogii.json`: well-grouped suffix holdout、hidden-test-safeなCode Competition。`TVT`は推論入力に使わない。

`submission.upload_mode` は外部提出の扱いを固定します。`manual`（既定）はローカル検証と提出物作成だけを行い、LB提出は人間に引き渡します。`disabled` は外部提出を禁止します。`agent` はKaggle系で、契約が明示し、`external_upload_requires_user_confirmation=true`を設定した場合だけ許可されます。共通CLIにはネットワーク送信機能がなく、`submission-plan`は常に非送信のhandoffを返します。

共通validatorは競技名を分岐条件にしません。競技固有のexact replay、hidden safety、外部API、simulation engineは、`policy.required_checks`にvalidatorの実行証跡を登録し、未実行ならgateでblockedにします。

パスは各コンペリポジトリをルートにした相対パスの例です。実データの所在・締切・規約が変わった場合は、履歴を上書きせず新しい契約版を作成してください。
