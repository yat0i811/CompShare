/**
 * アップロードから圧縮完了までの進捗を段階ごとに表示するパネル。
 *
 * 全段階を通した単一の 0-100% にはしない。
 * 各段階の所要時間が桁違い（送信は数分、サーバー取得は約1分、CPU圧縮は20〜30分）で、
 * 統合すると数十分間 1% も動かない区間ができ、「固まった」ように見えるため。
 *
 * UI 規約（docs\APP_STANDARD.md §9）:
 *  - 絵文字を使わない
 *  - テキストラベルのみ
 *  - 色は必ず CSS 変数経由。ライト/ダーク両方で判読できること
 */

// 表示順。サーバーから来る phase 名と一致させること。
const STAGES = [
  { key: "sending", label: "送信中" },
  { key: "starting", label: "開始待ち" },
  { key: "queued", label: "順番待ち" },
  { key: "fetching", label: "サーバー取得中" },
  { key: "encoding", label: "圧縮中" },
  { key: "storing", label: "保存中" },
];

function formatEta(sec) {
  if (sec === null || sec === undefined || !isFinite(sec) || sec < 0) return null;
  if (sec < 60) return `残り約 ${sec} 秒`;
  const min = Math.round(sec / 60);
  if (min < 60) return `残り約 ${min} 分`;
  const hour = Math.floor(min / 60);
  return `残り約 ${hour} 時間 ${min % 60} 分`;
}

export default function ProgressPanel({ stage, errorMessage }) {
  if (!stage || stage.name === "idle") return null;

  const isError = stage.name === "error";
  const isDisconnected = stage.name === "disconnected";
  const isDone = stage.name === "done";
  const currentIndex = STAGES.findIndex((s) => s.key === stage.name);

  let headline;
  if (isError) {
    headline = errorMessage || "エラーが発生しました。";
  } else if (isDisconnected) {
    headline = "サーバーからの応答がありません。処理は継続している可能性があります。ページを再読み込みすると状況を確認できます。";
  } else if (isDone) {
    headline = "圧縮が完了しました。";
  } else if (stage.name === "queued") {
    headline = stage.queuePosition
      ? `他の圧縮処理の完了を待っています（${stage.queuePosition} 番目）`
      : "他の圧縮処理の完了を待っています";
  } else if (stage.name === "starting") {
    headline = "送信が完了しました。サーバーの応答を待っています。";
  } else {
    const label = STAGES[currentIndex] ? STAGES[currentIndex].label : "処理中";
    headline = `${label} ${stage.percent}%`;
  }

  const eta = !isError && !isDisconnected && !isDone ? formatEta(stage.etaSec) : null;
  const showBar = !isError && !isDisconnected && currentIndex >= 0 && stage.name !== "queued";

  const statusClass = isError || isDisconnected ? "ng" : isDone ? "ok" : "running";

  return (
    <div className="progress-panel" role="status" aria-live="polite">
      <ol className="stage-list">
        {STAGES.map((s, i) => {
          const state = isDone || (currentIndex >= 0 && i < currentIndex)
            ? "past"
            : i === currentIndex
              ? "current"
              : "future";
          return (
            <li key={s.key} className={state} aria-current={state === "current" ? "step" : undefined}>
              {s.label}
            </li>
          );
        })}
      </ol>

      <p className={`headline ${statusClass}`}>{headline}</p>
      {eta && <p className="eta">{eta}</p>}

      {showBar && (
        <div className="bar-track">
          <div className="bar-fill" style={{ width: `${stage.percent}%` }} />
        </div>
      )}

      <style jsx>{`
        .progress-panel {
          margin-top: 16px;
        }
        .stage-list {
          display: flex;
          flex-wrap: wrap;
          gap: 12px;
          list-style: none;
          margin: 0 0 8px;
          padding: 0;
          font-size: 0.85rem;
        }
        /* 完了段階はチェックマーク等の記号ではなく文字の濃淡で表す */
        .stage-list .past {
          color: var(--muted);
        }
        .stage-list .current {
          color: var(--text);
          font-weight: 600;
        }
        .stage-list .future {
          color: var(--muted);
          opacity: 0.5;
        }
        .headline {
          margin: 0;
          font-size: 0.95rem;
        }
        .headline.running {
          color: var(--text);
        }
        .headline.ok {
          color: var(--ok);
        }
        .headline.ng {
          color: var(--ng);
        }
        .eta {
          margin: 4px 0 0;
          font-size: 0.85rem;
          color: var(--muted);
        }
        .bar-track {
          margin-top: 8px;
          width: 100%;
          height: 8px;
          background: var(--panel-alt);
          border: 1px solid var(--panel-border);
          border-radius: 4px;
          overflow: hidden;
        }
        .bar-fill {
          height: 100%;
          background: var(--accent);
          transition: width 0.3s ease;
        }
      `}</style>
    </div>
  );
}
