import logging
import json
import os
from flask import Flask, render_template, request, redirect, url_for, flash
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.trace import SpanKind
from opentelemetry.metrics import get_meter_provider, set_meter_provider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
import time
from opentelemetry.semconv.trace import SpanAttributes

# Flask App Initialization
app = Flask(__name__)
app.secret_key = 'secret'
COURSE_FILE = 'course_catalog.json'


logging.basicConfig(
    filename='app.log',
    level=logging.INFO,
    format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)

# OpenTelemetry Setup
resource = Resource.create({"service.name": "course-catalog-service"})
trace.set_tracer_provider(TracerProvider(resource=resource))
tracer = trace.get_tracer(__name__)

# Set up the Jaeger exporter
Jaeger_exporter = JaegerExporter(
    agent_host_name='localhost', 
    agent_port=6831,  
)
span_processor = BatchSpanProcessor(Jaeger_exporter)
trace.get_tracer_provider().add_span_processor(span_processor)

FlaskInstrumentor().instrument_app(app)

@app.before_request
def add_ip_to_span():
    current_span = trace.get_current_span()
    if current_span:
        current_span.set_attribute("http.client_ip", request.remote_addr)
        current_span.set_attribute("http.request_id", str(time.time()))  # Unique identifier for each request

meter_provider = MeterProvider()
set_meter_provider(meter_provider)
meter = get_meter_provider().get_meter("course_catalog_metrics")

# Create counters and histograms for metrics
route_counter = meter.create_counter(
    "route_requests",
    description="Number of requests for each route"
)

operation_time = meter.create_histogram(
    "operation_duration",
    description="Time taken for operations",
    unit="ms"
)

error_counter = meter.create_counter(
    "errors",
    description="Number of errors"
)

@app.before_request
def before_request():
    request.start_time = time.time()

@app.after_request
def after_request(response):
    route_counter.add(1, {"route": request.endpoint})  
    if hasattr(request, 'start_time'):
        duration = (time.time() - request.start_time) * 1000  
        operation_time.record(duration, {"route": request.endpoint})  
        current_span = trace.get_current_span()
        current_span.set_attribute("route_processing_time_ms", duration)
        current_span.add_event(
            "request_processed",
            {"route": request.endpoint, "processing_time_ms": duration}
        )
        app.logger.info(f"Processed {request.endpoint} in {duration:.2f} ms | IP: {request.remote_addr}")
    return response


def load_courses():
    """Load courses from the JSON file."""
    if not os.path.exists(COURSE_FILE):
        return []
    with open(COURSE_FILE, 'r') as file:
        return json.load(file)

def save_courses(data):
    """Save new course data to the JSON file."""
    courses = load_courses()
    courses.append(data)
    with open(COURSE_FILE, 'w') as file:
        json.dump(courses, file, indent=4)

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/catalog')
def course_catalog():
    with tracer.start_as_current_span("course_catalog_route") as span:
        courses = load_courses()
        span.set_attribute("total_courses", len(courses))
        span.set_attribute("route", "/catalog")           
        app.logger.info(
            f"Course Catalog accessed | Total Courses: {len(courses)} | IP: {request.remote_addr}"
        )
        route_counter.add(1, {"route": "/catalog"})
    return render_template('course_catalog.html', courses=courses)

@app.route('/add_course', methods=['GET', 'POST'])
def add_course():
    with tracer.start_as_current_span("add_course_route") as span:
        span.set_attribute("client.ip", request.remote_addr)
        span.set_attribute("client.host", request.host)
        
        if request.method == 'POST':
            required_fields = ['code', 'name']
            missing_fields = [field for field in required_fields if not request.form.get(field)]
            if missing_fields:
                error_message = f"Missing fields: {', '.join(missing_fields)}"
                error_counter.add(1, {"route": "/add_course", "error_type": "missing_fields"})
                
                with tracer.start_as_current_span(
                    "form_validation_error",
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "error.type": "missing_fields",
                        "missing_fields": str(missing_fields),
                        "total_errors": len(missing_fields),
                        "operation.type": "form_validation",
                        "client.ip": request.remote_addr,  
                        "client.host": request.host
                    }
                ) as error_span:
                    error_span.set_status(trace.Status(trace.StatusCode.ERROR))
                    error_span.record_exception(ValueError(error_message))
                    error_span.add_event(
                        "validation_failed",
                        attributes={
                            "error_count": len(missing_fields),
                            "timestamp": time.time(),
                            "fields_missing": str(missing_fields),
                            "client.ip": request.remote_addr 
                        }
                    )
                    
                    app.logger.error(
                        json.dumps({
                            "event": "form_validation_error",
                            "error_type": "missing_fields",
                            "missing_fields": missing_fields,
                            "total_errors": len(missing_fields),
                            "ip_address": request.remote_addr,
                            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S')
                        })
                    )
                
                span.set_attribute("error", True)
                span.set_attribute("error_type", "missing_fields")
                span.add_event("error_occurred", {"message": error_message})
                app.logger.error(f"Form validation failed - {error_message} | IP: {request.remote_addr}")
                flash(error_message, "error")
                return redirect(url_for('add_course'))
            
            course = {
                'code': request.form['code'],
                'name': request.form['name'],
                'instructor': request.form['instructor'],
                'semester': request.form['semester'],
                'schedule': request.form['schedule'],
                'classroom': request.form['classroom'],
                'prerequisites': request.form['prerequisites'],
                'grading': request.form['grading'],
                'description': request.form['description']
            }
            save_courses(course)
            span.set_attribute("added_course", course['name'])
            app.logger.info(
                f"Course added | Code: {course['code']} | Name: {course['name']} | IP: {request.remote_addr}"
            )
            flash(f"Course '{course['name']}' added successfully!", "success")
            return redirect(url_for('course_catalog'))
        app.logger.info("Rendered Add Course page")
    return render_template('add_course.html')


@app.route('/course/<code>')
def course_details(code):
    with tracer.start_as_current_span("course_details_route") as span:
        span.set_attribute("client.ip", request.remote_addr)
        span.set_attribute("client.host", request.host)
        span.set_attribute("request.headers", str(dict(request.headers)))
        
        courses = load_courses()
        course = next((course for course in courses if course['code'] == code), None)
        if not course:
            error_counter.add(1, {"route": "/course/<code>", "error_type": "not_found"})
            flash(f"No course found with code '{code}'.", "error")
            return redirect(url_for('course_catalog'))
            
        with tracer.start_as_current_span(
            f"view_course_{code}",
            kind=SpanKind.INTERNAL,
            attributes={
                "course.code": code,
                "course.name": course['name'],
                "course.instructor": course['instructor'],
                "course.semester": course['semester'],
                "operation.type": "course_view",
                "client.ip": request.remote_addr,  # Add IP to course view span
                "client.host": request.host
            }
        ) as course_span:
            course_span.add_event(
                "course_accessed",
                attributes={
                    "course.code": code,
                    "timestamp": time.time(),
                    "client.ip": request.remote_addr  
                }
            )
            
            span.set_attribute("viewed_course", course['name'])
            app.logger.info(
                f"Course Details Viewed | Code: {code} | Name: {course['name']} | IP: {request.remote_addr}"
            )
            
    return render_template('course_details.html', course=course)

if __name__ == '__main__':
    port = 5000
    host = '127.0.0.1'
    print(f"Server running at: http://{host}:{port}")
    app.run(debug=True, host=host, port=port)
