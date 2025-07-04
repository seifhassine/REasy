/**
 * REasy Community Template API – Cloud Functions v2
 * -------------------------------------------------
 * • All routes require a verified-email Firebase user
 * • Per-IP flood control via express-rate-limit
 * • /templates/:id/download
 *       → returns signed URL
 *       → bumps total downloadCnt
 *       → PER-USER CAP: max 3 downloads / template / 24 h
 */

const {onRequest} = require("firebase-functions/v2/https");
const {logger} = require("firebase-functions");
const admin = require("firebase-admin");
const express = require("express");
const rateLimit = require("express-rate-limit");

admin.initializeApp();
const db = admin.firestore();
const bucket = admin.storage().bucket();
const app = express();
app.use(express.json());

/* ─────────────── Configurable limits ─────────────── */
const MAX_DL_PER_DAY = 3;
const WINDOW_MS = 24 * 60 * 60 * 1e3;

/* ─────────────── Global flood control ────────────── */
app.use(
    rateLimit({
      windowMs: 15 * 60 * 1e3,
      max: 300,
      standardHeaders: true,
      legacyHeaders: false,
    }),
);

/* ─────────── Auth + verified-e-mail guard ─────────── */
async function requireVerifiedUser(req, res, next) {
  const hdr = req.get("Authorization") || "";
  const idToken = hdr.startsWith("Bearer ") ? hdr.substring(7) : null;
  if (!idToken) return res.status(401).json({message: "login required"});

  try {
    const decoded = await admin.auth().verifyIdToken(idToken);
    if (!decoded.email_verified) {
      return res.status(403).json({message: "verify e-mail first"});
    }
    req.user = decoded;
    next();
  } catch {
    return res.status(401).json({message: "invalid token"});
  }
}

/* ══════════════════  ROUTES  ════════════════════════ */

/* POST /templates – create metadata */
app.post("/templates", requireVerifiedUser, async (req, res) => {
  const {name, description, tags, game, registry, gsPath} = req.body;
  if (!name || !gsPath || !game) {
    return res.status(400).json({message: "name, game & gsPath required"});
  }

  try {
    const displayName = req.user.name || req.user.email || "Unknown";

    const docData = {
      name,
      description: description || "",
      tags: tags || [],
      game,
      registry: registry,
      gsPath,
      uploaderUid: req.user.uid,
      uploaderName: displayName,
      createdAt: admin.firestore.FieldValue.serverTimestamp(),
      downloadCnt: 0,
      ratings: [],
      avgRating: 0,
    };

    const doc = await db.collection("templates").add(docData);
    res.status(201).json({id: doc.id});
  } catch (e) {
    logger.error(e);
    res.sendStatus(500);
  }
});

/* GET /templates – list / search */
app.get("/templates", requireVerifiedUser, async (req, res) => {
  const {sortBy = "rating", limit = 50, search, game} = req.query;

  try {
    const ref = db.collection("templates");

    if (game) {
      const snapshots = await ref.where("game", "==", game).get();
      let docs = snapshots.docs;

      if (sortBy === "downloads") {
        docs.sort((a, b) => (b.data().downloadCnt || 0) - (a.data().downloadCnt || 0));
      } else if (sortBy === "date") {
        docs.sort((a, b) => {
          const bTime = b.data().createdAt || {};
          const aTime = a.data().createdAt || {};
          return (bTime.seconds || 0) - (aTime.seconds || 0);
        });
      } else {
        docs.sort((a, b) => {
          const bRating = b.data().avgRating;
          const aRating = a.data().avgRating;
          return (bRating || 0) - (aRating || 0);
        });
      }

      if (search) {
        const q = String(search).toLowerCase();
        docs = docs.filter((doc) => {
          const data = doc.data();
          return (
            (data.name || "").toLowerCase().includes(q) ||
            (data.description || "").toLowerCase().includes(q) ||
            (data.tags || []).some((tag) => tag.toLowerCase().includes(q))
          );
        });
      }

      const list = docs.slice(0, +limit).map((doc) => ({
        id: doc.id,
        ...doc.data(),
      }));

      return res.json({templates: list});
    }

    const docs = await ref.orderBy("createdAt", "desc").limit(+limit * 2).get();

    let list = docs.docs.map((d) => {
      const dt = d.data();
      const avg = dt.avgRating ?? 0;
      return {id: d.id, avgRating: avg, ...dt};
    });

    if (search) {
      const q = String(search).toLowerCase();
      list = list.filter(
          (t) =>
            (t.name || "").toLowerCase().includes(q) ||
          (t.description || "").toLowerCase().includes(q) ||
          (t.tags || []).some((tg) => tg.toLowerCase().includes(q)),
      );
    }

    if (sortBy === "downloads") {
      list.sort((a, b) => (b.downloadCnt || 0) - (a.downloadCnt || 0));
    } else if (sortBy === "date") {
      list.sort((a, b) => {
        const bTimestamp = b.createdAt || {};
        const aTimestamp = a.createdAt || {};
        return (bTimestamp.seconds || 0) - (aTimestamp.seconds || 0);
      });
    } else {
      list.sort((a, b) => (b.avgRating || 0) - (a.avgRating || 0));
    }

    res.json({templates: list.slice(0, +limit)});
  } catch (e) {
    logger.error(e);
    res.sendStatus(500);
  }
});

/* GET /templates/:id – return single template (+signed URL) */
app.get("/templates/:id", requireVerifiedUser, async (req, res) => {
  try {
    const snap = await db.collection("templates").doc(req.params.id).get();
    if (!snap.exists) return res.sendStatus(404);

    const data = snap.data();
    let fileUrl = null;
    try {
      const [url] = await bucket.file(data.gsPath).getSignedUrl({
        action: "read",
        expires: Date.now() + 60 * 60 * 1e3,
      });
      fileUrl = url;
    } catch (err) {
      logger.warn("signed-url error:", err.message);
    }
    res.json({...data, fileUrl});
  } catch (e) {
    logger.error(e);
    res.sendStatus(500);
  }
});

/* POST /templates/:id/rate – 1-to-5 stars */
app.post("/templates/:id/rate", requireVerifiedUser, async (req, res) => {
  const stars = Number(req.body.rating);
  if (![1, 2, 3, 4, 5].includes(stars)) {
    return res.status(400).json({message: "rating 1-5"});
  }

  const tplRef = db.collection("templates").doc(req.params.id);
  try {
    await db.runTransaction(async (tx) => {
      const snap = await tx.get(tplRef);
      if (!snap.exists) throw new Error("nf");

      const data = snap.data();
      const map = data.ratingsMap || {};
      map[req.user.uid] = stars;

      const arr = Object.values(map);
      const avg = arr.reduce((a, b) => a + b, 0) / arr.length;

      tx.update(tplRef, {
        ratingsMap: map,
        ratings: arr,
        avgRating: avg,
      });
    });
    res.json({message: "rating saved"});
  } catch (e) {
    logger.error(e);
    res.sendStatus(500);
  }
});

/* POST /templates/:id/comment */
app.post("/templates/:id/comment", requireVerifiedUser, async (req, res) => {
  const text = String(req.body.text || "").trim();
  if (!text) return res.status(400).json({message: "empty comment"});

  const comment = {
    uid: req.user.uid,
    username: req.user.name || req.user.email,
    text,
    timestamp: admin.firestore.Timestamp.now(),
  };

  try {
    await db
        .collection("templates")
        .doc(req.params.id)
        .update({
          comments: admin.firestore.FieldValue.arrayUnion(comment),
        });
    res.json({message: "comment added"});
  } catch (e) {
    logger.error(e);
    res.sendStatus(500);
  }
});

/* POST /templates/:id/download – bump counter with per-user cap */
app.post("/templates/:id/download", requireVerifiedUser, async (req, res) => {
  const tplId = req.params.id;
  const uid = req.user.uid;
  const now = Date.now();

  const statsRef = db
      .collection("templates").doc(tplId)
      .collection("downloaders").doc(uid);

  try {
    await db.runTransaction(async (tx) => {
      const statSnap = await tx.get(statsRef);
      let count = 0; let last = 0;
      if (statSnap.exists) {
        const d = statSnap.data();
        count = d.count ?? 0;
        last = d.lastDownload?.toMillis() ?? 0;
      }

      if (count >= MAX_DL_PER_DAY && now - last < WINDOW_MS) {
        throw new Error("OVER_LIMIT");
      }

      tx.set(statsRef,
          {count: count + 1,
            lastDownload: admin.firestore.Timestamp.fromMillis(now)},
          {merge: true});

      tx.update(db.collection("templates").doc(tplId), {
        downloadCnt: admin.firestore.FieldValue.increment(1),
      });
    });

    const tplSnap = await db.collection("templates").doc(tplId).get();
    if (!tplSnap.exists) return res.sendStatus(404);

    const tplData = tplSnap.data();
    let fileUrl = null;

    try {
      // Get the file from storage
      const file = bucket.file(tplData.gsPath);
      const [fileContents] = await file.download();

      try {
        const fileJson = JSON.parse(fileContents.toString("utf8"));

        fileJson.registry = tplData.registry || fileJson.registry || "default.json";

        const tempFilePath = `/tmp/${tplId}-${Date.now()}.json`;
        await admin.storage().bucket().file(tempFilePath).save(
            JSON.stringify(fileJson, null, 2),
            {contentType: "application/json"},
        );

        const [url] = await admin.storage().bucket().file(tempFilePath).getSignedUrl({
          action: "read",
          expires: Date.now() + 60 * 60 * 1e3,
        });

        fileUrl = url;

        setTimeout(async () => {
          try {
            await admin.storage().bucket().file(tempFilePath).delete();
          } catch (e) {
            logger.warn("Failed to delete temporary file:", e);
          }
        }, 60 * 60 * 1000);
      } catch (jsonErr) {
        logger.error("Error processing template JSON:", jsonErr);
        const [url] = await file.getSignedUrl({
          action: "read",
          expires: Date.now() + 60 * 60 * 1e3,
        });
        fileUrl = url;
      }
    } catch (err) {
      logger.error("signed-url error:", err);
    }

    return res.json({fileUrl});
  } catch (e) {
    if (e.message === "OVER_LIMIT") {
      return res.status(429).json({
        message: `Limit of ${MAX_DL_PER_DAY} downloads / 24 h reached`,
      });
    }
    logger.error(e);
    return res.sendStatus(500);
  }
});

exports.api = onRequest({region: "us-east1", invoker: "public"}, app);
