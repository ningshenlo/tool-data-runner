import copy
import json
import unittest
from unittest.mock import patch
import httpx
import test_taxonomy_batch as fixtures
import taxonomy_batch as batch
from jev import JEV_MODEL, JevError, call_jev, validate_response
from jev_capabilities import build_capability_plan, decode_capabilities, evaluate_capability_plan
from jev_taxonomy import execute_queued_capabilities
from taxonomy_shadow import TaxonomyTerm

SOURCE="Generate images from text prompts with our AI image generator."
PROFILE={"primary_job":{"value":"Generate images from text"},"capabilities_raw":[{"value":"text-to-image", "evidence":[{"quote":SOURCE,"node_id":"hero"}]}]}
TERM=TaxonomyTerm(term_id=3,dimension="capability",slug="text-to-image",name="Text to image",taxonomy_version=2)
def reply(request, role="core", confidence=.99):
    answers={}
    for key, question in request["questions"].items():
        selected=role if key.startswith("role_") else ("none" if role in {"absent","unclear"} else "s0")
        answers[key]={"type":"choice","choice":selected,"confidence":confidence,
                      "probabilities":{k:int(k==selected) for k in question["criteria"]}}
    return {"model":request["model"],"answers":answers,"usage":{"input_tokens":100,"output_tokens":0}}

class JevClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan=build_capability_plan(PROFILE,[TERM],SOURCE)
        self.request=self.plan["requests"][0]

    def test_grounding_and_whitelist(self):
        with self.assertRaisesRegex(JevError,"missing_capability_evidence"):
            build_capability_plan(PROFILE,[TERM],"A different product has no AI image features.")
        for role in ("core","supporting","integration","absent"):
            result=decode_capabilities(self.plan,[validate_response(self.request,reply(self.request,role))],.85)
            labels=result["capability_slugs"]
            if role=="absent": self.assertEqual(labels,[])
            else:
                self.assertEqual(labels[0]["slug"],"text-to-image")
                self.assertEqual(labels[0]["role"],role)
                self.assertEqual(labels[0]["evidence"],[{"quote":SOURCE,"node_id":"hero"}])

    def test_uncertain_absence_and_conflicting_proof_fall_back(self):
        for role in ("core","absent","unclear"):
            with self.assertRaisesRegex(JevError,"uncertain_capability"):
                decode_capabilities(self.plan,[reply(self.request,role,.4)],.85)
        raw=reply(self.request,"absent");raw["answers"]["proof_0"]=reply(self.request)["answers"]["proof_0"]
        with self.assertRaisesRegex(JevError,"conflicting_capability"):
            decode_capabilities(self.plan,[raw],.85)

    def test_malformed_response_rejected(self):
        for kind in ("model","choice","probabilities","confidence","usage"):
            raw=reply(self.request)
            if kind=="model":raw["model"]="other"
            if kind=="choice":raw["answers"]["role_0"]["choice"]="invented"
            if kind=="probabilities":raw["answers"]["role_0"]["probabilities"]["core"]=0
            if kind=="confidence":raw["answers"]["role_0"]["confidence"]=True
            if kind=="usage":raw["usage"]["input_tokens"]=-1
            with self.assertRaises(JevError):validate_response(self.request,raw)

    async def test_chunking_and_attempt_accounting_on_failure(self):
        terms=[TaxonomyTerm(term_id=i,dimension="capability",slug=f"cap-{i}",name=f"Cap {i}") for i in range(25)]
        plan=build_capability_plan(PROFILE,terms,SOURCE)
        self.assertEqual([len(r["questions"]) for r in plan["requests"]],[24,24,2])
        attempts=[];responses=[];calls=0
        async def provider(*args):
            nonlocal calls
            calls+=1
            if calls==2:raise JevError("jev_http_429")
            return reply(args[2],"absent")
        with patch("jev_capabilities.call_jev",provider), self.assertRaisesRegex(JevError,"jev_http_429"):
            await evaluate_capability_plan(plan,None,"key",responses=responses,attempts=attempts)
        self.assertEqual(len(attempts),2);self.assertEqual(len(responses),1)
        self.assertFalse(attempts[-1]["usage_known"])

    async def test_http_error_is_sanitized_and_never_retried(self):
        calls=0
        def handler(request):
            nonlocal calls
            calls+=1
            return httpx.Response(429,text="private provider message",request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaisesRegex(JevError,"^jev_http_429$"):
                await call_jev(client,"test-key",self.request)
        self.assertEqual(calls,1)

    async def test_http_contract_and_bounds(self):
        def handler(request):
            self.assertEqual(str(request.url),"https://api.typesafe.ai/v1/systemone")
            self.assertEqual(request.headers["authorization"],"Bearer test-key")
            return httpx.Response(200,json=reply(json.loads(request.content)),request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual((await call_jev(client,"test-key",self.request))["usage"]["input_tokens"],100)
            with self.assertRaisesRegex(JevError,"request_too_large"):
                await call_jev(client,"test-key",{**self.request,"state":"x"*110001})
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text="x"*262145))) as client:
            with self.assertRaisesRegex(JevError,"response_too_large"):await call_jev(client,"key",self.request)

class JevQueueTests(unittest.IsolatedAsyncioTestCase):
    setUp=fixtures.TaxonomyBatchStateMachineTests.setUp
    tearDown=fixtures.TaxonomyBatchStateMachineTests.tearDown

    async def prepare(self):
        self.config.taxonomy_capability_provider="jev";self.config.jev_api_key="test-key"
        cursor=self.connection.execute("""INSERT INTO taxonomy_batch_items
          (tool_id,pipeline_version,prompt_version,taxonomy_version,status,current_stage,source_url,source_text,
           source_content_hash,profile_json,model_trace_json,existing_entity_kind,existing_entity_source)
          VALUES(10,'jev-test','jev-test',2,'running','capability','https://acme.example',?,'hash',?,?,'independent_product','auto')""",
          (SOURCE,json.dumps(PROFILE),json.dumps({"mode":"capability_only","existing_market_slugs":["image-generators"]})))
        self.connection.commit()
        await batch.enqueue_stage(self.d1,self.config,self.catalog,item_id=cursor.lastrowid,stage="capability")
        return cursor.lastrowid

    async def test_success_uses_actual_jev_request_and_persists_provider(self):
        await self.prepare()
        async def provider(client,key,request,timeout):return reply(request)
        with patch("jev_capabilities.call_jev",provider):
            counts=await batch.submit_queued_batches(self.d1,self.config,self.catalog,object())
        self.assertEqual(counts["jev_completed"],1);self.assertEqual(counts["jev_requests"],1)
        row=self.connection.execute("SELECT * FROM taxonomy_batch_requests").fetchone()
        self.assertEqual(row["model"],JEV_MODEL);self.assertEqual(row["status"],"succeeded")
        self.assertEqual(json.loads(row["request_json"])["provider"],"jev")
        self.assertNotIn("test-key",row["request_json"]+row["response_json"])
        run=self.connection.execute("SELECT provider FROM classification_runs").fetchone()
        self.assertEqual(run["provider"],"typesafe_jev")
        assignment=self.connection.execute("SELECT evidence_json FROM product_taxonomy_assignments WHERE term_id=3").fetchone()
        self.assertIn(SOURCE,assignment["evidence_json"])

    async def test_manual_capability_assignment_survives_jev(self):
        await self.prepare()
        self.connection.execute("INSERT INTO product_taxonomy_assignments(tool_id,term_id,is_primary,confidence,decision_status,source,evidence_json,assigned_at,updated_at) VALUES(10,3,0,1,'verified','manual','{}','2026','2026')")
        self.connection.commit()
        async def provider(client,key,request,timeout):return reply(request,"supporting")
        with patch("jev_capabilities.call_jev",provider):
            await execute_queued_capabilities(self.d1,self.config,self.catalog)
        row=self.connection.execute("SELECT source,decision_status,confidence,evidence_json FROM product_taxonomy_assignments WHERE term_id=3").fetchone()
        self.assertEqual((row["source"],row["decision_status"],row["confidence"]),("manual","verified",1))
        self.assertEqual(json.loads(row["evidence_json"]),{})

    async def test_uncertainty_falls_back_once_with_usage_retained(self):
        await self.prepare()
        async def provider(client,key,request,timeout):return reply(request,confidence=.4)
        with patch("jev_capabilities.call_jev",provider) as mocked:
            counts=await execute_queued_capabilities(self.d1,self.config,self.catalog)
            again=await execute_queued_capabilities(self.d1,self.config,self.catalog)
        self.assertEqual(counts["jev_fallback"],1);self.assertEqual(counts["jev_input_tokens"],100)
        self.assertEqual(again["jev_requests"],0)
        rows=self.connection.execute("SELECT model,status FROM taxonomy_batch_requests ORDER BY id").fetchall()
        self.assertEqual([(r["model"],r["status"]) for r in rows],[(JEV_MODEL,"failed"),("gpt-5.6-luna","queued")])
        self.assertEqual(batch.provider_for_trace([dict(r) for r in rows]),"hybrid_jev_openai")

    async def test_http_failure_counted_and_fallback_can_submit_to_openai(self):
        await self.prepare()
        async def provider(*args):raise JevError("jev_http_503")
        class OpenAI:
            async def upload_jsonl(self,*args):return {"id":"file-fallback"}
            async def create_batch(self,*args,**kwargs):return {"id":"batch-fallback","status":"validating"}
        with patch("jev_capabilities.call_jev",provider):
            counts=await batch.submit_queued_batches(self.d1,self.config,self.catalog,OpenAI())
        self.assertEqual(counts["jev_requests"],1);self.assertEqual(counts["jev_fallback"],1)
        self.assertEqual(counts["requests_submitted"],1)

    async def test_interrupted_job_recovers_after_provider_rollback(self):
        await self.prepare()
        old="2020-01-01T00:00:00.000Z"
        job=self.connection.execute("INSERT INTO taxonomy_batch_jobs(stage,model,reasoning_effort,status,created_at,updated_at) VALUES('capability',?,'none','in_progress',?,?)",(JEV_MODEL,old,old)).lastrowid
        self.connection.execute("UPDATE taxonomy_batch_requests SET job_id=?,status='submitted'",(job,));self.connection.commit()
        self.config.taxonomy_capability_provider="openai"
        counts=await execute_queued_capabilities(self.d1,self.config,self.catalog)
        self.assertEqual(counts["jev_recovered"],1)
        rows=self.connection.execute("SELECT model,status FROM taxonomy_batch_requests ORDER BY id").fetchall()
        self.assertEqual([(r["model"],r["status"]) for r in rows],[(JEV_MODEL,"failed"),("gpt-5.6-luna","queued")])

    async def test_crash_during_recovery_can_resume(self):
        await self.prepare()
        old="2020-01-01T00:00:00.000Z"
        job=self.connection.execute("INSERT INTO taxonomy_batch_jobs(stage,model,reasoning_effort,status,error,created_at,updated_at) VALUES('capability',?,'none','failed','jev_interrupted',?,?)",(JEV_MODEL,old,old)).lastrowid
        self.connection.execute("UPDATE taxonomy_batch_requests SET job_id=?,status='submitted'",(job,));self.connection.commit()
        counts=await execute_queued_capabilities(self.d1,self.config,self.catalog)
        self.assertEqual(counts["jev_recovered"],1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM taxonomy_batch_requests WHERE status='queued' AND model='gpt-5.6-luna'").fetchone()[0],1)

    def test_capability_does_not_relabel_primary_model(self):
        self.assertEqual(batch.primary_model_for_trace([{"stage":"leaf","model":"gpt-5.6-luna"},{"stage":"capability","model":JEV_MODEL}]),"gpt-5.6-luna")

if __name__=="__main__":unittest.main()
