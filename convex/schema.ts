import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  clusters: defineTable({
    stableKey: v.string(),
    name: v.string(),
    description: v.string(),
    volume: v.number(),
    severity: v.union(v.literal("low"), v.literal("medium"), v.literal("high")),
    ticketIds: v.array(v.number()),
    rank: v.number(),
    updatedAt: v.number(),
  }).index("by_stable_key", ["stableKey"]),

  macros: defineTable({
    stableKey: v.string(),
    clusterKey: v.string(),
    title: v.string(),
    response: v.string(),
    qaScore: v.number(),
    version: v.number(),
    status: v.string(),
    sourceTicketId: v.number(),
    ratingCount: v.optional(v.number()),
    averageRating: v.optional(v.number()),
    updatedAt: v.number(),
  })
    .index("by_stable_key", ["stableKey"])
    .index("by_cluster_key", ["clusterKey"]),

  ratings: defineTable({
    macroKey: v.string(),
    rating: v.number(),
    feedback: v.string(),
    createdAt: v.number(),
  }).index("by_macro_key", ["macroKey"]),

  runEvents: defineTable({
    stableKey: v.string(),
    clusterKey: v.string(),
    agentName: v.string(),
    stepType: v.string(),
    detail: v.string(),
    createdAt: v.number(),
  })
    .index("by_stable_key", ["stableKey"])
    .index("by_created_at", ["createdAt"]),
});
