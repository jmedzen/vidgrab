interface Props {
  /** 0–100；null = 不顯示 */
  percent: number | null
}

/** 視窗頂端那條細進度條（下載時才出現）。 */
export default function ProgressBar({ percent }: Props) {
  const on = percent !== null
  return (
    <div id="progressbar" className={on ? 'on' : ''}>
      <div id="progressfill" style={{ width: `${on ? percent : 0}%` }} />
    </div>
  )
}
