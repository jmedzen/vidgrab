interface Props {
  open: boolean
  qr: string
  status: string
  onClose: () => void
}

export default function LoginModal({ open, qr, status, onClose }: Props) {
  return (
    <div className={`modal${open ? ' show' : ''}`} id="loginmodal" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <h3>掃碼登入 B 站</h3>
        <p>手機 B 站 App →「我的」→ 右上角「掃一掃」<br />掃完在手機上按確認</p>
        {qr ? <img id="qrimg" src={qr} alt="B 站登入 QR" /> : null}
        <div className="stat" id="loginstat">{status}</div>
      </div>
    </div>
  )
}
