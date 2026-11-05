interface Props {
  message: string
}

export default function Toast({ message }: Props) {
  return (
    <div className={`toast${message ? ' show' : ''}`} id="toast"
         role="status" aria-live="assertive">
      {message}
    </div>
  )
}
