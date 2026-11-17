/**
 * One icon per concept, used everywhere in both apps (design rule): a reading is
 * always a droplet, an alert always a ringing bell, and so on.
 */
import {
  BellRing,
  BookOpenText,
  ClipboardCheck,
  Droplet,
  Gauge,
  LayoutDashboard,
  MessageCircle,
  Mic,
  ScrollText,
  Siren,
  Stethoscope,
  UserRound,
  CircleUserRound,
} from "lucide-react";

export const Icons = {
  reading: Droplet,
  limits: Gauge,
  alert: BellRing,
  handoff: Stethoscope,
  review: ClipboardCheck,
  kb: BookOpenText,
  audit: ScrollText,
  patient: UserRound,
  voice: Mic,
  emergency: Siren,
  chat: MessageCircle,
  overview: LayoutDashboard,
  account: CircleUserRound,
} as const;
