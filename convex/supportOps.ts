import { mutation, query } from "./_generated/server";
import { v } from "convex/values";

const clusterValidator = v.object({
  stableKey: v.string(),
  name: v.string(),
  description: v.string(),
  volume: v.number(),
  severity: v.union(v.literal("low"), v.literal("medium"), v.literal("high")),
  ticketIds: v.array(v.number()),
  rank: v.number(),
  updatedAt: v.number(),
});

const macroValidator = v.object({
  stableKey: v.string(),
  clusterKey: v.string(),
  title: v.string(),
  response: v.string(),
  qaScore: v.number(),
  version: v.number(),
  status: v.string(),
  sourceTicketId: v.number(),
  updatedAt: v.number(),
});

const eventValidator = v.object({
  stableKey: v.string(),
  clusterKey: v.string(),
  agentName: v.string(),
  stepType: v.string(),
  detail: v.string(),
  createdAt: v.number(),
});

export const syncSnapshot = mutation({
  args: {
    clusters: v.array(clusterValidator),
    macros: v.array(macroValidator),
    events: v.array(eventValidator),
  },
  handler: async (ctx, args) => {
    for (const cluster of args.clusters) {
      const existing = await ctx.db
        .query("clusters")
        .withIndex("by_stable_key", (q) => q.eq("stableKey", cluster.stableKey))
        .unique();
      if (existing) await ctx.db.patch(existing._id, cluster);
      else await ctx.db.insert("clusters", cluster);
    }

    for (const macro of args.macros) {
      const existing = await ctx.db
        .query("macros")
        .withIndex("by_stable_key", (q) => q.eq("stableKey", macro.stableKey))
        .unique();
      if (existing) {
        const version = existing.response === macro.response
          ? existing.version
          : existing.version + 1;
        await ctx.db.patch(existing._id, {
          ...macro,
          version,
          ratingCount: existing.ratingCount,
          averageRating: existing.averageRating,
        });
      } else {
        await ctx.db.insert("macros", macro);
      }
    }

    for (const event of args.events) {
      const existing = await ctx.db
        .query("runEvents")
        .withIndex("by_stable_key", (q) => q.eq("stableKey", event.stableKey))
        .unique();
      if (existing) await ctx.db.patch(existing._id, event);
      else await ctx.db.insert("runEvents", event);
    }

    return {
      clusters: args.clusters.length,
      macros: args.macros.length,
      events: args.events.length,
    };
  },
});

export const dashboard = query({
  args: {},
  handler: async (ctx) => {
    const clusters = (await ctx.db.query("clusters").collect())
      .sort((a, b) => a.rank - b.rank);
    const macros = await ctx.db.query("macros").collect();
    const events = (await ctx.db.query("runEvents").collect())
      .sort((a, b) => b.createdAt - a.createdAt)
      .slice(0, 30);
    return { clusters, macros, events };
  },
});

export const rateMacro = mutation({
  args: {
    macroKey: v.string(),
    rating: v.number(),
    feedback: v.string(),
  },
  handler: async (ctx, args) => {
    if (!Number.isInteger(args.rating) || args.rating < 1 || args.rating > 5) {
      throw new Error("rating must be an integer from 1 to 5");
    }
    const macro = await ctx.db
      .query("macros")
      .withIndex("by_stable_key", (q) => q.eq("stableKey", args.macroKey))
      .unique();
    if (!macro) throw new Error(`Unknown macro: ${args.macroKey}`);

    await ctx.db.insert("ratings", {
      macroKey: args.macroKey,
      rating: args.rating,
      feedback: args.feedback,
      createdAt: Date.now(),
    });
    const ratings = await ctx.db
      .query("ratings")
      .withIndex("by_macro_key", (q) => q.eq("macroKey", args.macroKey))
      .collect();
    const averageRating = ratings.reduce((sum, item) => sum + item.rating, 0) / ratings.length;
    await ctx.db.patch(macro._id, {
      ratingCount: ratings.length,
      averageRating,
      status: averageRating < 3 ? "needs_revision" : macro.status,
    });
    return { ok: true, ratingCount: ratings.length, averageRating };
  },
});
