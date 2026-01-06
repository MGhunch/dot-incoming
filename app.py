from flask import Flask, request, jsonify
from flask_cors import CORS
from anthropic import Anthropic
import httpx
import json
import os
from datetime import datetime, timedelta

app = Flask(__name__)
CORS(app)

# Anthropic client
client = Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

# Airtable config
AIRTABLE_API_KEY = os.environ.get('AIRTABLE_API_KEY')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'app8CI7NAZqhQ4G1Y')
CLIENTS_TABLE = 'Clients'
PROJECTS_TABLE = 'Projects'
TRACKER_TABLE = 'Tracker'

# Month/Quarter mappings
MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
MONTH_TO_FULL = {
    'Jan': 'January', 'Feb': 'February', 'Mar': 'March', 'Apr': 'April',
    'May': 'May', 'Jun': 'June', 'Jul': 'July', 'Aug': 'August',
    'Sep': 'September', 'Oct': 'October', 'Nov': 'November', 'Dec': 'December'
}
MONTH_TO_QUARTER = {
    'Jan': 'Jan-Mar', 'Feb': 'Jan-Mar', 'Mar': 'Jan-Mar',
    'Apr': 'Apr-Jun', 'May': 'Apr-Jun', 'Jun': 'Apr-Jun',
    'Jul': 'Jul-Sep', 'Aug': 'Jul-Sep', 'Sep': 'Jul-Sep',
    'Oct': 'Oct-Dec', 'Nov': 'Oct-Dec', 'Dec': 'Oct-Dec'
}
QUARTER_TO_COLUMN = {
    'Jan-Mar': 'JAN-MAR',
    'Apr-Jun': 'APR-JUN',
    'Jul-Sep': 'JUL-SEP',
    'Oct-Dec': 'OCT-DEC'
}


def get_target_month(parsed_month):
    """Determine target month - use parsed month or default to 2 weeks from now"""
    if parsed_month and parsed_month in MONTH_NAMES:
        return parsed_month
    
    # Default: 2 weeks from today
    target_date = datetime.now() + timedelta(weeks=2)
    return MONTH_NAMES[target_date.month - 1]

# Claude prompt for parsing incoming job requests
PARSE_PROMPT = """You are Dot Incoming, a pre-triage assistant for Hunch creative agency.

Your job is to extract client, project, timing, and owner information from minimal input. People will send brief messages like:
- "New job for Sky - Box Colours"
- "Heads up - Tower want something on sustainability for Feb"
- "Fisher Funds - annual report refresh, Sarah's leading it"
- "incoming from one nz, campaign refresh next month"

CLIENTS (match fuzzy - people use nicknames):
- ONE = One NZ, One, One New Zealand, One NZ Marketing, One Marketing
- ONB = One NZ Business, One Business
- ONS = One NZ Simplification, One Simplification
- SKY = Sky, Sky TV, Sky Television
- TOW = Tower, Tower Insurance
- FIS = Fisher, Fisher Funds
- FST = Firestop
- EON = Eon, Eon Fibre
- LAB = Labour

OWNERS (match fuzzy to these known contacts):
- Tower: Paige Buckland, Dino Ligouri
- One NZ Marketing: Katherine Kazalbash, Jess Downey
- One NZ Business: Keith Goi, Salah Mohammed
- One NZ Simplification: Tracey Barclay
- Fisher Funds: Jade Jordan
- Sky: (various)

EXTRACT:
1. clientCode - The 3-letter code (ONE, ONB, ONS, SKY, TOW, FIS, FST, EON, LAB)
2. clientName - Full client name
3. projectName - Whatever description is given (clean it up if needed)
4. month - Target month if mentioned (Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec), or null if not mentioned
5. owner - Person's name if mentioned, or null if not mentioned

MONTH HINTS:
- "Feb" or "February" → "Feb"
- "next month" → calculate from current date
- "end of Q1" → "Mar"
- Nothing mentioned → null (system will default to 2 weeks out)

RESPOND WITH JSON ONLY:
{
    "clientCode": "SKY",
    "clientName": "Sky TV",
    "projectName": "Box Colours",
    "month": "Feb",
    "owner": null
}
"""


def get_next_job_number(client_code):
    """Look up client in Airtable, get next job number, increment it"""
    if not AIRTABLE_API_KEY:
        return None, "No Airtable API key"
    
    headers = {
        'Authorization': f'Bearer {AIRTABLE_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    try:
        # Find client record
        search_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CLIENTS_TABLE}"
        params = {'filterByFormula': f"{{Client code}}='{client_code}'"}
        
        response = httpx.get(search_url, headers=headers, params=params, timeout=10.0)
        response.raise_for_status()
        
        records = response.json().get('records', [])
        
        if not records:
            return None, f"Client '{client_code}' not found"
        
        record = records[0]
        record_id = record['id']
        fields = record['fields']
        
        current_number = fields.get('Next #', 1)
        job_number = f"{client_code} {str(current_number).zfill(3)}"
        
        # Increment the number
        update_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CLIENTS_TABLE}/{record_id}"
        update_data = {'fields': {'Next #': current_number + 1}}
        
        httpx.patch(update_url, headers=headers, json=update_data, timeout=10.0)
        
        return job_number, None
        
    except Exception as e:
        return None, str(e)


def create_project(job_number, client_name, project_name):
    """Create a new project record in Airtable"""
    if not AIRTABLE_API_KEY:
        return None, "No Airtable API key"
    
    headers = {
        'Authorization': f'Bearer {AIRTABLE_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    try:
        create_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PROJECTS_TABLE}"
        
        project_data = {
            'fields': {
                'Job Number': job_number,
                'Project Name': project_name,
                'Status': 'Incoming',
                'Stage': 'Triage'
            }
        }
        
        response = httpx.post(create_url, headers=headers, json=project_data, timeout=10.0)
        response.raise_for_status()
        
        return response.json(), None
        
    except Exception as e:
        return None, str(e)


def create_tracker(project_record_id, project_name, client_name, month, owner):
    """Create a tracker record with $5K ballpark in the right quarter"""
    if not AIRTABLE_API_KEY:
        print("TRACKER ERROR: No Airtable API key")
        return None, "No Airtable API key"
    
    headers = {
        'Authorization': f'Bearer {AIRTABLE_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    try:
        create_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{TRACKER_TABLE}"
        
        # Determine quarter and column
        quarter = MONTH_TO_QUARTER.get(month, 'Jan-Mar')
        quarter_column = QUARTER_TO_COLUMN.get(quarter, 'JAN-MAR')
        full_month = MONTH_TO_FULL.get(month, month)
        
        tracker_data = {
            'fields': {
                'Job Number': [project_record_id],  # Linked record field
                'Spend type': 'Project budget',
                'Description': project_name,
                'Month': full_month,
                'Quarter': quarter,
                'Ballpark': True,
                'Status': 'Active',
                quarter_column: 5000
            }
        }
        
        print(f"TRACKER: Creating record with data: {tracker_data}")
        
        response = httpx.post(create_url, headers=headers, json=tracker_data, timeout=10.0)
        
        print(f"TRACKER: Response status: {response.status_code}")
        print(f"TRACKER: Response body: {response.text}")
        
        response.raise_for_status()
        
        return response.json(), None
        
    except Exception as e:
        print(f"TRACKER ERROR: {str(e)}")
        return None, str(e)


@app.route('/incoming', methods=['POST'])
def incoming():
    """Process an incoming job request"""
    try:
        data = request.get_json()
        
        # Handle both Remote (JSON) and Email (via Traffic) inputs
        if 'message' in data:
            input_text = data['message']
        elif 'emailContent' in data:
            input_text = data['emailContent']
        elif 'text' in data:
            input_text = data['text']
        else:
            return jsonify({'error': 'No input provided'}), 400
        
        # Check if client was provided directly (from Remote dropdown)
        provided_client_code = data.get('clientCode')
        provided_client_name = data.get('clientName')
        
        # Include current date for Claude's context
        current_date = datetime.now().strftime('%B %d, %Y')
        
        # Parse with Claude (still need to extract project name, month, owner)
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=500,
            messages=[
                {'role': 'user', 'content': f'Today is {current_date}.\n\n{PARSE_PROMPT}\n\nInput: {input_text}'}
            ]
        )
        
        content = response.content[0].text
        
        # Clean up response (remove markdown if present)
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0]
        elif '```' in content:
            content = content.split('```')[1].split('```')[0]
        
        parsed = json.loads(content.strip())
        
        # Use provided client if available, otherwise use parsed
        if provided_client_code:
            client_code = provided_client_code
            client_name = provided_client_name or parsed.get('clientName', '')
        else:
            client_code = parsed.get('clientCode')
            client_name = parsed.get('clientName', '')
        
        project_name = parsed.get('projectName', 'TBC')
        parsed_month = parsed.get('month')
        owner = parsed.get('owner')
        
        # Check if we identified a client
        if not client_code:
            return jsonify({
                'success': False,
                'error': 'Could not identify client',
                'message': 'Which client is this for?',
                'parsed': parsed
            }), 200
        
        # Get target month (parsed or 2 weeks from now)
        target_month = get_target_month(parsed_month)
        
        # Get next job number
        job_number, error = get_next_job_number(client_code)
        
        if error:
            return jsonify({
                'success': False,
                'error': error,
                'parsed': parsed
            }), 200
        
        # Create the project
        project, error = create_project(job_number, client_name, project_name)
        
        if error:
            return jsonify({
                'success': False,
                'error': f'Failed to create project: {error}',
                'jobNumber': job_number,
                'parsed': parsed
            }), 200
        
        # Create the tracker record (using project record ID for linked field)
        project_record_id = project.get('id')
        tracker, tracker_error = create_tracker(project_record_id, project_name, client_name, target_month, owner)
        
        if tracker_error:
            # Project created but tracker failed - still return success but note the issue
            return jsonify({
                'success': True,
                'jobNumber': job_number,
                'projectName': project_name,
                'clientCode': client_code,
                'clientName': client_name,
                'month': target_month,
                'owner': owner,
                'recordId': project.get('id'),
                'trackerWarning': f'Tracker not created: {tracker_error}'
            })
        
        # Full success!
        return jsonify({
            'success': True,
            'jobNumber': job_number,
            'projectName': project_name,
            'clientCode': client_code,
            'clientName': client_name,
            'month': target_month,
            'owner': owner,
            'recordId': project.get('id'),
            'trackerId': tracker.get('id')
        })
        
    except json.JSONDecodeError as e:
        return jsonify({
            'success': False,
            'error': f'Failed to parse Claude response: {str(e)}'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'service': 'dot-incoming'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
