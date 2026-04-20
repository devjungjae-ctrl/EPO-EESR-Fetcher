import os
import re
import time
import base64
import requests
import xml.etree.ElementTree as ET
import pandas as pd
import fitz  # PyMuPDF
import google.generativeai as genai

# ==========================================
# 0. API & 환경 설정
# ==========================================
# 1) Google Gemini API 
# 실제 구동을 위해 환경 변수나 로컬 파일에서 읽도록 세팅 (Github 유출 방지)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "발급받은_GEMINI_API_KEY_여기에_붙여넣기")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel('gemini-1.5-flash-latest')

# 2) EPO OPS API 
EPO_CONSUMER_KEY = os.environ.get("EPO_CONSUMER_KEY", "발급받은_EPO_APP_KEY")
EPO_CONSUMER_SECRET = os.environ.get("EPO_CONSUMER_SECRET", "발급받은_EPO_SECRET_KEY")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "EESR_Downloads_API")
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# XML 네임스페이스 매핑
NS = {
    'ops': 'http://ops.epo.org',
    'xlink': 'http://www.w3.org/1999/xlink',
    'exch': 'http://www.epo.org/exchange'
}

# ==========================================
# 1. EPO OPS 통신 모듈
# ==========================================
def get_epo_token(client_id, client_secret):
    """EPO OPS API OAuth 토큰 발급"""
    try:
        url = "https://ops.epo.org/3.2/auth/accesstoken"
        auth_string = f"{client_id}:{client_secret}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()
        
        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"grant_type": "client_credentials"}
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as e:
        print(f"[ERROR] EPO 토큰 발급 실패: {e}")
        return None

def find_publication_for_application(app_number, token):
    """출원번호(예: 18817854)를 이용해 공개/등록 문헌번호 및 Kind 코드 추출"""
    url = f"http://ops.epo.org/3.2/rest-services/published-data/search"
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/xml'}
    # 파라미터로 출원번호 매핑 방어식 구성 (예: ap=EP18817854)
    query_num = app_number if str(app_number).upper().startswith("EP") else f"EP{app_number}"
    
    response = requests.get(url, headers=headers, params={'q': f'ap={query_num}'})
    if response.status_code != 200:
        return None, None
        
    try:
        root = ET.fromstring(response.content)
        # 첫 번째 검색결과의 doc-number 및 kind 획득
        doc_element = root.find('.//ops:search-result/ops:publication-reference/exch:document-id[@document-id-type="docdb"]', NS)
        if doc_element is not None:
            doc_num = doc_element.find('exch:doc-number', NS).text
            kind = doc_element.find('exch:kind', NS).text
            return doc_num, kind
    except Exception as e:
        print(f"   [오류] 문헌번호 파싱 실패: {e}")
    return None, None

def get_image_details_for_publication(doc_num, kind, token):
    """문헌번호를 통해 EESR이 포함될 가능성이 높은 문헌 원본의 이미지 다운로드 링크 목록 확보"""
    url = f"http://ops.epo.org/3.2/rest-services/published-data/publication/docdb/EP.{doc_num}.{kind}/images"
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/xml'}
    
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None, None

    try:
        root = ET.fromstring(response.content)
        # SearchReport 또는 FullDocument 추출
        # 보통 SearchReport가 분리된 A3나, FullDocument 끝에 붙은 형태(A1)를 스캔
        link = None
        pages = 0
        
        for instance in root.findall('.//ops:document-instance', NS):
            desc = instance.attrib.get('desc')
            if desc in ('SearchReport', 'FullDocument'):
                link = instance.attrib.get('link')
                pages = int(instance.attrib.get('number-of-pages', 0))
                if desc == 'SearchReport':
                   break # SearchReport 전용 문서가 있으면 최고 우선순위
                   
        return link, pages
    except Exception as e:
        print(f"   [오류] 문서 이미지 탐색 실패: {e}")
    return None, None

def download_eesr_pdf_from_ops(app_number, token, save_dir):
    """
    EPO OPS 표준 로직에 따라 문서를 스캔하고 다운로드합니다.
    (API 트래픽 제한을 위해 EESR이 위치하는 문서의 페이지들을 합병하거나 텍스트 직접 축출)
    """
    print(f"  -> [{app_number}] EPO OPS API 서지 매핑 중...")
    app_num_clean = str(app_number).split('.')[0]
    
    doc_num, kind = find_publication_for_application(app_num_clean, token)
    if not doc_num or not kind:
        print(f"  -> 공개 문헌을 찾을 수 없습니다. (EPO 서버에 존재하지 않음)")
        return None
        
    print(f"  -> 문헌 확인됨: EP{doc_num}{kind}. 원본 문서 구조 파악 중...")
    link, max_pages = get_image_details_for_publication(doc_num, kind, token)
    
    if not link or max_pages == 0:
        print(f"  -> 다운로드 가능한 원본(EESR) 링크가 존재하지 않습니다.")
        return None
        
    print(f"  -> 문서 획득 중 (총 {max_pages} 페이지). PDF 텍스트 추출 조립 중...")
    
    # 트래픽 및 시간 한계상 EESR은 보통 후반 부 페이지(최대 5페이지)에 위치.
    # WIPO/EPO 규격에 따라 FullDocument의 경우 마지막 1~5 페이지만 뽑고, SearchReport면 전부 뽑습니다.
    target_pages = []
    if "search-report" in link.lower() or max_pages <= 6:
        # 분리형 리포트거나, 페이지가 매우 적으면 처음부터 끝까지 추출
        target_pages = range(1, max_pages + 1)
    else:
        # A1 문헌 등 페이지가 10장이 넘어가면 뒷부분에 EESR 존재
        start_page = max(1, max_pages - 4)
        target_pages = range(start_page, max_pages + 1)

    full_text_extracted = ""
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/pdf'}
    
    for page in target_pages:
        page_url = f"http://ops.epo.org/3.2/rest-services/{link}?Range={page}"
        res = requests.get(page_url, headers=headers)
        if res.status_code == 200:
            try:
                # Byte Stream에서 바로 텍스트 추출 (디스크 I/O 최적화)
                doc = fitz.Document(stream=res.content, filetype="pdf")
                for p in doc:
                    full_text_extracted += p.get_text("text") + "\n"
                doc.close()
            except:
                pass
        time.sleep(0.5) # 초당 OPS Rate Limit 방어

    # 병합된 텍스트가 의미 있는 수준인지 확인
    if len(full_text_extracted) > 100:
        return full_text_extracted
    else:
        return None

# ==========================================
# 2. NLP (Gemini) 분기 처리
# ==========================================
def analyze_with_gemini(text):
    prompt = f"""
    아래는 유럽 특허청(EPO)의 Search Report 또는 관련 오피스 액션의 추출 텍스트입니다.
    이 텍스트에 "Article 84" (또는 Art 84, A. 84, A84 등) 법규에 기반한 거절 또는 지적 사항이 명시되어 있는지 파악하고,
    있다면 반드시 아래 4가지 중 **정확히 하나**로만 대답하세요. 부연 설명은 1문장으로만 추가하세요.
    없다면 "Article 84 없음" 이라고 답하세요.
    
    1. 명확성 부족 (Lack of Clarity)
    2. 명세서에 의한 뒷받침 부족 (Lack of Support)
    3. 간결성 및 청구항 수 (Conciseness & Number of Claims)
    4. 기타 (Other)
    
    [텍스트 추출본 (요약)]
    {text[-8000:]}
    """
    try:
        res = gemini_model.generate_content(prompt)
        text_result = res.text.strip()
        if "명확성 부족" in text_result or "Lack of Clarity" in text_result: return "명확성 부족"
        elif "뒷받침 부족" in text_result or "Lack of Support" in text_result: return "뒷받침 부족"
        elif "간결성" in text_result or "Conciseness" in text_result: return "간결성"
        elif "기타" in text_result or "Other" in text_result: return "기타"
        else: return "Article 84 없음"
    except Exception as e:
        return f"분류 에러 ({e})"

# ==========================================
# 3. Main Routine
# ==========================================
def main():
    print("==========================================================")
    print(" EPO OPS 개발자 API 기반 EESR 파이프라인 가동 준비 중... ")
    print("==========================================================")
    
    excel_path = os.path.join(BASE_DIR, '..', '특허검색_EESR검색.xlsx')
    if not os.path.exists(excel_path):
        df = pd.DataFrame({'출원번호': ['18817854.5']})
    else:
        df = pd.read_excel(excel_path)
        
    if '거절사유_분류(EPO_API)' not in df.columns:
        df['거절사유_분류(EPO_API)'] = ""
        
    # 토큰 발급
    print("[시스템] EPO OPS OAuth 토큰 요청 중...")
    token = get_epo_token(EPO_CONSUMER_KEY, EPO_CONSUMER_SECRET)
    if not token:
        print("[시스템 에러] API 토큰 발급에 실패하여 프로그램을 종료합니다.")
        return
    print("[시스템] EPO Token 획득 성공! 파싱 궤도에 진입합니다.\n")
    
    for idx, row in df.iterrows():
        app_num = str(row['출원번호']).strip()
        if not app_num or app_num.lower() == 'nan': continue
        
        print(f"[진행도: {idx+1}/{len(df)}] EPO 문헌 탐색: {app_num}")
        
        extracted_text = download_eesr_pdf_from_ops(app_num, token, DOWNLOAD_DIR)
        
        if extracted_text is None:
            df.at[idx, '거절사유_분류(EPO_API)'] = "탐색 불가 (자료 없음)"
            continue
            
        # Article 84 존재 유무 스캐닝
        pattern = r"\b(?:Article|Art\.?|A\.?)\s*84\b"
        if re.search(pattern, extracted_text, re.IGNORECASE):
            print("  -> [HIT] Article 84 검출! Gemini AI 분석 요청 중...")
            time.sleep(2) # Gemini 제어 Limit 보호
            cls_result = analyze_with_gemini(extracted_text)
            print(f"  -> 분석 결과: {cls_result}")
            df.at[idx, '거절사유_분류(EPO_API)'] = cls_result
        else:
            print("  -> [PASS] Article 84 거절 사유가 문서에 존재하지 않습니다.")
            df.at[idx, '거절사유_분류(EPO_API)'] = "거절이력 없음"
                
    result_path = os.path.join(BASE_DIR, '특허검색_EESR검색_EPO_API_결과.xlsx')
    df.to_excel(result_path, index=False)
    print(f"\n[성공] EPO 공식 플랫폼 기반 추출이 완료되었습니다: {result_path}")

if __name__ == "__main__":
    main()
