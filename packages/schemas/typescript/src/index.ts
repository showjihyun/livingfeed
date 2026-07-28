/**
 * Living Feed 스키마 — TypeScript 타입 (코드젠 산출물 re-export).
 * 재생성: pnpm --filter @livingfeed/schemas generate
 * 수동 타입 작성 금지 (ADR-001).
 */
export type { EventEnvelope, Provenance } from "./generated/envelope.schema";
export type { ActorActionPerformed } from "./generated/actor.action.performed.schema";
export type { ActorDecisionMade } from "./generated/actor.decision.made.schema";
export type { ActorEmotionShifted } from "./generated/actor.emotion.shifted.schema";
export type { ActorMessageSent } from "./generated/actor.message.sent.schema";
export type { FeedPostPublished } from "./generated/feed.post.published.schema";
export type { PlayerCommentPosted } from "./generated/player.comment.posted.schema";
export type { PlayerDmSent } from "./generated/player.dm.sent.schema";
export type { PlayerReactionAdded } from "./generated/player.reaction.added.schema";
export type { SystemTickCompleted } from "./generated/system.tick.completed.schema";
