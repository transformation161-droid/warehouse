-- PR40 manual administrator-only PostgreSQL change, with prior verified backup/restore.
-- Schema v4 remains: no tables altered. Feature marker requires explicit runtime opt-in.
BEGIN;
SET LOCAL search_path=ksu,pg_catalog;
SELECT pg_advisory_xact_lock(170009);
DO $$ BEGIN
 IF (SELECT coalesce(max(version),0) FROM ksu.schema_version) <> 4
   OR to_regprocedure('ksu.inventory_count_version()') IS NOT NULL THEN
  RAISE EXCEPTION 'unexpected schema or inventory feature already installed';
 END IF;
END $$;

CREATE OR REPLACE FUNCTION record_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=ksu,pg_catalog AS $$
DECLARE role text:=actor_role(); n jsonb:=NEW.data::jsonb; o jsonb;
BEGIN
 IF role IS NULL THEN RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.author_id IS DISTINCT FROM actor_id() OR
    (role='inspector' AND (NEW.collection IN ('sites','questions') OR NOT accessible(NEW.site_id))) OR
    (NEW.collection='inventories' AND n->>'stage' IS DISTINCT FROM 'primary') THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
 ELSE
  o:=OLD.data::jsonb;
  IF (NEW.author_id,NEW.collection,NEW.id,NEW.created_at) IS DISTINCT FROM (OLD.author_id,OLD.collection,OLD.id,OLD.created_at)
    OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
  IF role='inspector' AND (NEW.collection IN ('sites','questions','inspections') OR NOT accessible(OLD.site_id) OR NOT accessible(NEW.site_id)
    OR (NEW.collection NOT IN ('visits','inventoryLines') AND (NEW.site_id IS DISTINCT FROM OLD.site_id OR OLD.author_id<>actor_id()))
     OR (NEW.collection='inventoryLines' AND NEW.site_id IS DISTINCT FROM OLD.site_id)
    OR (NEW.collection='visits' AND (o->>'assignedTo' IS DISTINCT FROM actor_id() OR n->>'assignedTo' IS DISTINCT FROM actor_id()))
    OR (NEW.collection='inventories' AND (n->>'stage',n->>'decision') IS DISTINCT FROM (o->>'stage',o->>'decision'))) THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
 END IF;
 -- Cross-author inventory counting is granted only on a plan on an assigned site.
 -- The server additionally validates numbers, photos and optimistic versions.
 IF NEW.collection='inventoryLines' THEN
  IF NOT EXISTS (
   SELECT 1 FROM records p WHERE p.collection='inventories'
    AND p.id=n->>'inventoryId' AND p.site_id=NEW.site_id
    AND (p.data::jsonb->>'stage')='primary'
    AND (p.data::jsonb->>'status')='draft'
  ) THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND EXISTS(
     SELECT 1 FROM records p WHERE p.collection='inventories'
     AND p.id=n->>'inventoryId' AND coalesce(p.data::jsonb->>'sourceHash','')<>''
   ) AND (n - ARRAY['actual','reason','photo','countedBy','countedAt']) IS DISTINCT FROM
         (o - ARRAY['actual','reason','photo','countedBy','countedAt']) THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
  END IF;
  IF TG_OP='UPDATE' AND role='inspector' AND
   (OLD.author_id<>actor_id() OR EXISTS(
      SELECT 1 FROM records p WHERE p.collection='inventories'
      AND p.id=n->>'inventoryId' AND coalesce(p.data::jsonb->>'sourceHash','')<>''
   )) THEN
    IF NOT EXISTS(
      SELECT 1 FROM records p WHERE p.collection='inventories'
      AND p.id=n->>'inventoryId' AND coalesce(p.data::jsonb->>'sourceHash','')<>''
    ) OR (n - ARRAY['actual','reason','photo','countedBy','countedAt']) IS DISTINCT FROM
         (o - ARRAY['actual','reason','photo','countedBy','countedAt']) THEN
     RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
    END IF;
    IF n->>'actual' IS NOT NULL AND
       ((n->>'countedBy') IS DISTINCT FROM actor_id() OR coalesce(n->>'countedAt','')='') AND
       ((n->>'actual',n->>'reason',n->>'photo') IS DISTINCT FROM
        (o->>'actual',o->>'reason',o->>'photo')) THEN
     RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
    END IF;
  END IF;
 END IF;
 IF NEW.collection='inventories' AND TG_OP='UPDATE' THEN
  IF coalesce(o->>'sourceHash','')<>'' AND o->>'status'<>'final'
     AND n->>'stage' IS DISTINCT FROM o->>'stage' THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
  END IF;
  IF coalesce(o->>'sourceHash','')<>'' AND
    (n->>'siteId',n->>'date',n->>'title',n->>'plannedPositions',n->>'sourceHash',n->>'sourceName')
    IS DISTINCT FROM
    (o->>'siteId',o->>'date',o->>'title',o->>'plannedPositions',o->>'sourceHash',o->>'sourceName') THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
  END IF;
  IF o->>'status'='final' AND n->>'status'<>'final' THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
  IF n->>'status'='final' AND o->>'status'<>'final' THEN
   IF NOT EXISTS(SELECT 1 FROM records ln WHERE ln.collection='inventoryLines'
          AND ln.data::jsonb->>'inventoryId'=NEW.id) OR EXISTS(
      SELECT 1 FROM records ln WHERE ln.collection='inventoryLines'
      AND ln.data::jsonb->>'inventoryId'=NEW.id AND ln.data::jsonb->>'actual' IS NULL
   ) OR (SELECT count(*) FROM records ln WHERE ln.collection='inventoryLines'
      AND ln.data::jsonb->>'inventoryId'=NEW.id) <>
      (n->>'plannedPositions')::integer THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 IF NEW.collection='visits' THEN
  IF n->>'siteId' IS DISTINCT FROM NEW.site_id OR NOT EXISTS(SELECT 1 FROM users u WHERE u.id=n->>'assignedTo' AND active=1
    AND (u.role='manager' OR EXISTS(SELECT 1 FROM user_sites WHERE user_id=u.id AND site_id=NEW.site_id))) THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
  IF TG_OP='INSERT' THEN
   IF n->>'status' IS DISTINCT FROM 'planned' OR n->>'inspectionId' IS NOT NULL OR coalesce(n->>'cancellationReason','')<>''
     OR n->>'cancelledBy' IS NOT NULL OR n->>'cancelledAt' IS NOT NULL OR (role='inspector' AND n->>'assignedTo' IS DISTINCT FROM actor_id()) THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
  ELSE
   IF o->>'status' IS DISTINCT FROM 'planned' OR n->>'status' IS NULL OR n->>'status' NOT IN ('planned','completed','cancelled') THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
   IF n->>'status'<>'planned' AND (n->>'date',n->>'siteId',n->>'kind',n->>'assignedTo',coalesce(n->>'goal','')) IS DISTINCT FROM
     (o->>'date',o->>'siteId',o->>'kind',o->>'assignedTo',coalesce(o->>'goal','')) THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
   IF n->>'status'='cancelled' AND (length(trim(coalesce(n->>'cancellationReason',''))) NOT BETWEEN 1 AND 1200
      OR n->>'cancelledBy' IS DISTINCT FROM actor_id() OR coalesce(n->>'cancelledAt','')='') THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
   IF n->>'status'='completed' AND (NOT EXISTS(SELECT 1 FROM records ins WHERE ins.collection='inspections' AND ins.id=n->>'inspectionId'
     AND ins.site_id=NEW.site_id AND ins.author_id=n->>'assignedTo') OR EXISTS(SELECT 1 FROM records v WHERE v.collection='visits'
     AND v.id<>NEW.id AND v.data::jsonb->>'inspectionId'=n->>'inspectionId')) THEN
    RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
  END IF;
  IF n->>'status'<>'cancelled' AND (coalesce(n->>'cancellationReason','')<>'' OR n->>'cancelledBy' IS NOT NULL OR n->>'cancelledAt' IS NOT NULL)
    OR n->>'status'<>'completed' AND n->>'inspectionId' IS NOT NULL THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

ALTER POLICY records_update ON records
 USING (
  actor_role()='manager' OR
  (actor_role()='inspector' AND accessible(site_id) AND
   (collection='inventoryLines' OR
    CASE WHEN collection='visits' THEN data::jsonb->>'assignedTo'=actor_id()
    ELSE author_id=actor_id() END))
 )
 WITH CHECK(accessible(site_id) OR actor_role()='manager');

CREATE FUNCTION inventory_count_version() RETURNS integer
 LANGUAGE sql IMMUTABLE AS $$ SELECT 1 $$;
REVOKE ALL ON FUNCTION inventory_count_version() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION inventory_count_version() TO ksu_runtime;
COMMIT;
