import type { ReactNode } from "react";

export type IconName =
  | "search"
  | "library"
  | "chat"
  | "external"
  | "updates"
  | "chevron-left"
  | "chevron-right"
  | "arrow-left"
  | "arrow-up-right"
  | "plus"
  | "settings"
  | "send"
  | "folder-plus"
  | "refresh"
  | "copy"
  | "check";

const paths: Record<IconName, ReactNode> = {
  search: (
    <>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </>
  ),
  library: (
    <>
      <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" />
      <path d="M3.5 10h17" />
    </>
  ),
  chat: (
    <>
      <path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.7 8.7 0 0 1-3.5-.7L4 20l1.7-4.2A7.4 7.4 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z" />
      <path d="M8 10h8M8 14h5" />
    </>
  ),
  external: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 13v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5" />
    </>
  ),
  updates: (
    <>
      <path d="M20 11a8.1 8.1 0 0 0-14.9-4L3 10" />
      <path d="M3 4v6h6M4 13a8.1 8.1 0 0 0 14.9 4L21 14" />
      <path d="M21 20v-6h-6" />
    </>
  ),
  "chevron-left": <path d="m14.5 5-7 7 7 7" />,
  "chevron-right": <path d="m9.5 5 7 7-7 7" />,
  "arrow-left": <path d="M19 12H5m7 7-7-7 7-7" />,
  "arrow-up-right": <path d="M7 17 17 7M7 7h10v10" />,
  plus: <path d="M12 5v14M5 12h14" />,
  settings: (
    <>
      <path d="M4 6h16M4 12h16M4 18h16" />
      <circle cx="9" cy="6" r="2" />
      <circle cx="15" cy="12" r="2" />
      <circle cx="10" cy="18" r="2" />
    </>
  ),
  send: (
    <>
      <path d="m21 3-7.5 18-3.5-7-7-3.5Z" />
      <path d="M21 3 10 14" />
    </>
  ),
  "folder-plus": (
    <>
      <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" />
      <path d="M3.5 10h17M12 12v5M9.5 14.5h5" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 11a8.1 8.1 0 0 0-14.9-4L3 10" />
      <path d="M3 4v6h6M4 13a8.1 8.1 0 0 0 14.9 4L21 14" />
      <path d="M21 20v-6h-6" />
    </>
  ),
  copy: (
    <>
      <rect x="8" y="8" width="12" height="12" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
};

type IconProps = {
  name: IconName;
  size?: number;
  strokeWidth?: number;
  className?: string;
};

export default function Icon({ name, size = 18, strokeWidth = 1.7, className }: IconProps) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths[name]}
    </svg>
  );
}
