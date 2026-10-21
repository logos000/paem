// Inline stroke icons (24px grid, currentColor) — no icon dependency.
import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function Svg({ size = 18, children, ...rest }: P) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>
      {children}
    </svg>
  );
}

export const IconChat = (p: P) => <Svg {...p}><path d="M5 18.5V6.8A2.8 2.8 0 0 1 7.8 4h8.4A2.8 2.8 0 0 1 19 6.8v6.4a2.8 2.8 0 0 1-2.8 2.8H8.6L5 18.5Z" /></Svg>;
export const IconUpload = (p: P) => <Svg {...p}><path d="M12 15V4m0 0-4 4m4-4 4 4" /><path d="M5 15v2.2A2.8 2.8 0 0 0 7.8 20h8.4a2.8 2.8 0 0 0 2.8-2.8V15" /></Svg>;
export const IconGauge = (p: P) => <Svg {...p}><path d="M4 15a8 8 0 1 1 16 0" /><path d="m12 15 3.5-4.5" /><path d="M4 19h16" /></Svg>;
export const IconPlus = (p: P) => <Svg {...p}><path d="M12 5v14M5 12h14" /></Svg>;
export const IconArrowUp = (p: P) => <Svg {...p}><path d="M12 19V5m0 0-6 6m6-6 6 6" /></Svg>;
export const IconStop = (p: P) => <Svg {...p}><rect x="7" y="7" width="10" height="10" rx="2" /></Svg>;
export const IconCheck = (p: P) => <Svg {...p}><path d="m5 12.5 4.2 4.2L19 7" /></Svg>;
export const IconAlert = (p: P) => <Svg {...p}><path d="M12 9v4m0 3.2v.1" /><path d="M10.3 4.6 3.4 17a2 2 0 0 0 1.7 3h13.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0Z" /></Svg>;
export const IconMinus = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="8.5" /><path d="M8.5 12h7" /></Svg>;
export const IconCopy = (p: P) => <Svg {...p}><rect x="8.5" y="8.5" width="11" height="11" rx="2.4" /><path d="M15.5 8.5V6.8a2.3 2.3 0 0 0-2.3-2.3H6.8a2.3 2.3 0 0 0-2.3 2.3v6.4a2.3 2.3 0 0 0 2.3 2.3h1.7" /></Svg>;
export const IconThumbUp = (p: P) => <Svg {...p}><path d="M7.5 10.5v9H5a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h2.5Zm0 0 3.6-6.2a1.6 1.6 0 0 1 2.9 1.2l-.9 4h5a2 2 0 0 1 2 2.4l-1.2 6a2 2 0 0 1-2 1.6H7.5" /></Svg>;
export const IconThumbDown = (p: P) => <Svg {...p}><path d="M16.5 13.5v-9H19a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1h-2.5Zm0 0-3.6 6.2a1.6 1.6 0 0 1-2.9-1.2l.9-4H6a2 2 0 0 1-2-2.4l1.2-6a2 2 0 0 1 2-1.6h9.3" /></Svg>;
export const IconExternal = (p: P) => <Svg {...p}><path d="M14 4h6v6M20 4l-8.5 8.5" /><path d="M18 14v3.2a2.8 2.8 0 0 1-2.8 2.8H6.8A2.8 2.8 0 0 1 4 17.2V8.8A2.8 2.8 0 0 1 6.8 6H10" /></Svg>;
export const IconClose = (p: P) => <Svg {...p}><path d="M6 6l12 12M18 6 6 18" /></Svg>;
export const IconDoc = (p: P) => <Svg {...p}><path d="M14 3.5H8A2.5 2.5 0 0 0 5.5 6v12A2.5 2.5 0 0 0 8 20.5h8a2.5 2.5 0 0 0 2.5-2.5V8L14 3.5Z" /><path d="M14 3.5V8h4.5M9 12.5h6M9 16h4" /></Svg>;
export const IconSun = (p: P) => <Svg {...p}><circle cx="12" cy="12" r="3.8" /><path d="M12 2.8v1.8M12 19.4v1.8M4.9 4.9l1.3 1.3M17.8 17.8l1.3 1.3M2.8 12h1.8M19.4 12h1.8M4.9 19.1l1.3-1.3M17.8 6.2l1.3-1.3" /></Svg>;
export const IconMoon = (p: P) => <Svg {...p}><path d="M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z" /></Svg>;
export const IconMenu = (p: P) => <Svg {...p}><path d="M4 7h16M4 12h16M4 17h10" /></Svg>;
export const IconChevron = (p: P) => <Svg {...p}><path d="m9 6 6 6-6 6" /></Svg>;
export const IconRefresh = (p: P) => <Svg {...p}><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4" /></Svg>;
export const IconSearch = (p: P) => <Svg {...p}><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.3-4.3" /></Svg>;
export const IconLayers = (p: P) => <Svg {...p}><path d="m12 4 8.5 4.5L12 13 3.5 8.5 12 4Z" /><path d="m3.5 12.5 8.5 4.5 8.5-4.5M3.5 16.5 12 21l8.5-4.5" /></Svg>;
export const IconShield = (p: P) => <Svg {...p}><path d="M12 3.5 5 6.2v5.3c0 4.3 2.9 7.8 7 9 4.1-1.2 7-4.7 7-9V6.2l-7-2.7Z" /><path d="m9 12 2.2 2.2L15.5 10" /></Svg>;
export const IconScale = (p: P) => <Svg {...p}><path d="M12 4v16M7 20h10M5 7h14" /><path d="m5 7-2.5 6a2.8 2.8 0 0 0 5 0L5 7Zm14 0-2.5 6a2.8 2.8 0 0 0 5 0L19 7Z" /></Svg>;
export const IconQuote = (p: P) => <Svg {...p}><path d="M5 11.5h4.5V17H5v-5.5Zm0 0C5 8.5 6.3 7 9 6.5M14.5 11.5H19V17h-4.5v-5.5Zm0 0c0-3 1.3-4.5 4-5" /></Svg>;
export const IconArrowLeft = (p: P) => <Svg {...p}><path d="M19 12H5m0 0 6-6m-6 6 6 6" /></Svg>;
export const IconSettings = (p: P) => <Svg {...p}><path d="M4 7h10M18 7h2M4 17h2M10 17h10" /><circle cx="16" cy="7" r="2" /><circle cx="8" cy="17" r="2" /></Svg>;
