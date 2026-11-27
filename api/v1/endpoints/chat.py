from flask import Blueprint, jsonify, request, g
from api.core.database import get_db
from api.core.security import require_auth


bp = Blueprint('chat_v1', __name__, url_prefix='/api/v1/chat')



            
