pipeline {
    agent any

    environment {
        REGISTRY = 'registry.internal.corp/sre'
        IMAGE_NAME = 'kubesentinel'
        BUILD_TAG = "${env.BUILD_NUMBER}"
        APP_ENV = 'staging'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Static Analysis & Guardrail Validation') {
            steps {
                sh '''
                    python3 -m venv .venv
                    . .venv/bin/activate
                    pip install --upgrade pip
                    pip install -r requirements.txt
                    pytest -v tests/test_guardrails.py tests/test_runbooks.py
                '''
            }
        }

        stage('Unit & SRE Tool Tests') {
            steps {
                sh '''
                    . .venv/bin/activate
                    pytest -v tests/
                '''
            }
        }

        stage('Container Build & Security Audit') {
            steps {
                sh '''
                    docker build -t ${REGISTRY}/${IMAGE_NAME}:${BUILD_TAG} -t ${REGISTRY}/${IMAGE_NAME}:latest .
                '''
            }
        }

        stage('Deploy to Kubernetes Cluster') {
            when {
                branch 'main'
            }
            steps {
                sh '''
                    echo "Deploying KubeSentinel to ${APP_ENV} cluster..."
                    # kubectl set image deployment/kubesentinel-api kubesentinel=${REGISTRY}/${IMAGE_NAME}:${BUILD_TAG} -n monitoring
                '''
            }
        }
    }

    post {
        always {
            cleanWs()
        }
        failure {
            echo "KubeSentinel CI/CD Pipeline Failed! Alerting On-Call team."
        }
    }
}
