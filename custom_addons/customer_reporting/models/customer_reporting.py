import os
import json
import logging
import threading
import base64
import time
import redis
import boto3
from odoo import models, fields, api
import odoo

logger = logging.getLogger(__name__)

# Thread control variables
_worker_started = False
_worker_lock = threading.Lock()


def process_task(db_name, record_id, company_name):
    """
    Invokes Bedrock Agent to process research on the company, then commits the report back to Odoo.
    """
    registry = odoo.modules.registry.Registry(db_name)
    with registry.cursor() as cr:
        env = api.Environment(cr, odoo.SUPERUSER_ID, {})
        record = env['customer.reporting'].browse(record_id)
        
        try:
            redis_host = os.environ.get('REDIS_HOST', 'redis')
            redis_port = int(os.environ.get('REDIS_PORT', 6379))
            redis_client = redis.Redis(host=redis_host, port=redis_port, db=0)
            
            # Update Valkey status to processing
            redis_client.set(f"report_status_{record_id}", "Analysis in process")
            logger.info(f"Pushed 'Analysis in process' status to Valkey for record {record_id}")
            
            region = os.environ.get('AWS_REGION', 'ap-south-1')
            client = boto3.client('bedrock-agent-runtime', region_name=region)
            agent_id = os.environ.get('BEDROCK_AGENT_ID', 'DUMMY')
            agent_alias_id = os.environ.get('BEDROCK_AGENT_ALIAS_ID', 'DUMMY')
            
            logger.info(f"Triggering Bedrock AI invoke_agent for {company_name}")
            response = client.invoke_agent(
                agentId=agent_id,
                agentAliasId=agent_alias_id,
                sessionId=f"session-{record_id}",
                inputText=f"Research the company '{company_name}' and provide a comprehensive final report.",
                enableTrace=False
            )
            
            report_text = ""
            for event in response.get('completion'):
                if 'chunk' in event:
                    chunk_data = event['chunk']['bytes'].decode('utf-8')
                    report_text += chunk_data
            
            if report_text:
                record.report_file = base64.b64encode(report_text.encode('utf-8')).decode('utf-8')
                record.report_filename = f"{company_name.replace(' ', '_').lower()}_report.txt"
                redis_client.set(f"report_status_{record_id}", "Done")
                logger.info(f"Successfully processed Bedrock report for {company_name} and committed to Odoo database.")
            else:
                logger.warning(f"AI returned empty report for {company_name}")
                redis_client.set(f"report_status_{record_id}", "Failed: Processing error")
            
            # Commit Odoo transaction
            cr.commit()
            
        except Exception as e:
            logger.error("Bedrock task execution failed for record %s: %s", record_id, str(e), exc_info=True)
            # Try to report failure status to Redis/Valkey
            try:
                redis_host = os.environ.get('REDIS_HOST', 'redis')
                redis_port = int(os.environ.get('REDIS_PORT', 6379))
                redis_client = redis.Redis(host=redis_host, port=redis_port, db=0)
                redis_client.set(f"report_status_{record_id}", "Failed: Processing error")
            except Exception:
                pass


def valkey_consumer_loop():
    """
    Background worker thread running continuously to pull tasks from Valkey,
    invoke the AWS Bedrock Agent, and write back findings to Odoo.
    """
    logger.info("Valkey consumer loop started.")
    redis_host = os.environ.get('REDIS_HOST', 'redis')
    redis_port = int(os.environ.get('REDIS_PORT', 6379))
    
    while True:
        try:
            # Connect/reconnect to Valkey
            redis_client = redis.Redis(host=redis_host, port=redis_port, db=0)
            
            # Block on brpop for task queue (timeout 5s)
            result = redis_client.brpop('reporting_tasks', timeout=5)
            if result:
                _, task_data_bytes = result
                task_data = json.loads(task_data_bytes.decode('utf-8'))
                
                db_name = task_data.get('db_name')
                record_id = task_data.get('record_id')
                company_name = task_data.get('company_name')
                
                logger.info(f"Worker picked up task for record {record_id} ({company_name}) from Valkey.")
                
                # Execute Bedrock agent call
                process_task(db_name, record_id, company_name)
        except redis.ConnectionError as ce:
            logger.warning("Valkey connection failed in consumer loop, retrying in 10s: %s", str(ce))
            time.sleep(10)
        except Exception as e:
            logger.error("Error in Valkey consumer loop: %s", str(e), exc_info=True)
            time.sleep(2)


def start_valkey_worker():
    """
    Starts the Valkey worker background daemon thread if it is not already running in the current process.
    """
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        
        thread = threading.Thread(target=valkey_consumer_loop, name="ValkeyConsumerThread", daemon=True)
        thread.start()
        logger.info("Spawned Valkey consumer background thread.")


class CustomerReporting(models.Model):
    _name = "customer.reporting"
    _description = "Customer Reporting"
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string="Company Name", required=True, tracking=True)
    report_file = fields.Binary(string="End Result", attachment=True, tracking=True)
    report_filename = fields.Char(string="File Name")

    def action_get_report(self):
        # Start worker thread locally in this process if not already running
        start_valkey_worker()
        
        # Connect to Valkey to queue the task
        redis_host = os.environ.get('REDIS_HOST', 'redis')
        redis_port = int(os.environ.get('REDIS_PORT', 6379))
        redis_client = redis.Redis(host=redis_host, port=redis_port, db=0)
        
        for record in self:
            # Set status to Queued
            redis_client.set(f"report_status_{record.id}", "Queued")
            
            # Build and push task details to queue
            task_details = {
                'db_name': self.env.cr.dbname,
                'record_id': record.id,
                'company_name': record.name
            }
            redis_client.lpush('reporting_tasks', json.dumps(task_details))
            logger.info(f"Queued customer report task in Valkey for {record.name} (Record ID: {record.id})")
            
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Analysis Queued',
                'message': 'Analysis request queued in Valkey. It will be processed in the background.',
                'sticky': False,
                'type': 'info',
            }
        }

    @api.model
    def _cron_valkey_worker_heartbeat(self):
        """
        Scheduled action (cron) heartbeat to start/ensure the Valkey consumer thread runs
        periodically in the Odoo background cron process.
        """
        logger.info("Valkey worker heartbeat cron invoked.")
        start_valkey_worker()
