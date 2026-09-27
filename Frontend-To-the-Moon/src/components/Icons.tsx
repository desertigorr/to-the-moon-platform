export function MoonMark() {
  return (
    <svg
      viewBox="0 0 32 32"
      width="30"
      height="30"
      fill="none"
      aria-hidden="true"
    >
      <path
        d="M22.5 22.4A10 10 0 0 1 9.6 9.5 10 10 0 1 0 22.5 22.4Z"
        fill="currentColor"
      />
      <path
        d="M7 25 25 7m-7 0h7v7"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function BusGlyph() {
  return (
    <>
      <rect
        x="5"
        y="3"
        width="14"
        height="16"
        rx="3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
      />
      <path
        d="M5 12h14M8 19v2m8-2v2M8 7h8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <circle cx="8.5" cy="15.5" r="1" fill="currentColor" />
      <circle cx="15.5" cy="15.5" r="1" fill="currentColor" />
    </>
  )
}

export function BusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      aria-hidden="true"
    >
      <BusGlyph />
    </svg>
  )
}

export function SectionIcon({
  name,
}: {
  name: 'overview' | 'scenarios' | 'history' | 'network'
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {name === 'network' ? (
        <>
          <path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2V5Zm6-2v16m6-14v16" />
          <path d="m6 12 5-3 7 4" />
        </>
      ) : name === 'overview' ? (
        <>
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </>
      ) : name === 'scenarios' ? (
        <>
          <path d="M6 20V4m0 7c0 6 12 1 12 8M6 8c0 6 12 1 12-5" />
          <path d="m15 5 3-3 3 3m-6 12 3 3 3-3" />
        </>
      ) : (
        <>
          <path d="M3 11a9 9 0 1 1 2.3 7M3 5v6h6" />
          <path d="M12 7v5l3 2" />
        </>
      )}
    </svg>
  )
}
