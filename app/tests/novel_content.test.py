import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.api.novels import extract_paragraphs_from_soup
from bs4 import BeautifulSoup


test_data1 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of the chapter.</p>
        <p>This is the third paragraph of the chapter.</p>
        <p>This is the fourth paragraph of the chapter.</p>
        <p>This is the fifth paragraph of the chapter.</p>
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

test_data2 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of the chapter.</p>
        <p>This is the third paragraph of the chapter.</p>
        <p>This is the fourth paragraph of the chapter.</p>
        This is the fifth paragraph of the chapter.
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

test_data3 = {
    "chapter_title_text": "Chapter 1: The Beginning",
    "soup": """
    <div class="chapter-content">
        <h1>Chapter 1: The Beginning</h1>
        <p>This is the first paragraph of the chapter.</p>
        <p>This is the second paragraph of<em> the </em>chapter.</p>
        <p>This is the third paragraph of<em> the</em> chapter.</p>
        <p>This is the fourth <em>paragraph</em> of the chapter.</p>
        This is the fifth paragraph of <em>the</em> chapter.
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter 1: The Beginning",
        "paragraphs": [
            "This is the first paragraph of the chapter.",
            "This is the second paragraph of the chapter.",
            "This is the third paragraph of the chapter.",
            "This is the fourth paragraph of the chapter.",
            "This is the fifth paragraph of the chapter."
        ]
    }
}

test_data4 = {
    "chapter_title_text": "Chapter X: Meeting the Turtle",
    "soup": """
    <div class="chapter-content">
        <p>Su Yu said, "Senior—"</p>
        <p>"Just call me old turtle. There is no need to refer to me as a senior."</p>
        Su Yu continued, "Senior, if I recall correctly, the looping turtles are known for their incredible defense. Can I ask if that is due to natural talent or if it is a result of body forging? And how strong is the looping turtles' defenses compared with the iron devourers?"<p>The turtle blanked out slightly before answering, "It's hard to answer that. We specialize in defense, but that's mostly due to our natural talent and our shell. As for the iron devourers...they have probably obtained their strong defense from body forging."</p>
        <p>Su Yu asked in a straightforward manner, "Are you a Cloudbreach or a Mountainsea, Senior?"</p>
        <p>He couldn't see through the turtle's cultivation.</p>
        <p>"You can consider me an early Mountainsea."</p>
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter X: Meeting the Turtle",
        "paragraphs": [
            'Su Yu said, "Senior—"',
            '"Just call me old turtle. There is no need to refer to me as a senior."',
            'Su Yu continued, "Senior, if I recall correctly, the looping turtles are known for their incredible defense. Can I ask if that is due to natural talent or if it is a result of body forging? And how strong is the looping turtles\' defenses compared with the iron devourers?"',
            'The turtle blanked out slightly before answering, "It\'s hard to answer that. We specialize in defense, but that\'s mostly due to our natural talent and our shell. As for the iron devourers...they have probably obtained their strong defense from body forging."',
            'Su Yu asked in a straightforward manner, "Are you a Cloudbreach or a Mountainsea, Senior?"',
            "He couldn't see through the turtle's cultivation.",
            '"You can consider me an early Mountainsea."'
        ]
    }
}

test_data5 = {
    "chapter_title_text": "Chapter Y: Little Fatty",
    "soup": """
    <div class="chapter-content">
        <p>Zhu Tiandao smiled and said, "Half a student is still a student, right? Of course, Silk Destroying King is definitely uninterested in formally acknowledging this student. It's fine. Su Yu is quite an arrogant kid as well. He might not be willing-cough, cough. My apologies. I should watch my words. Silk Destroying King, please don't mind me..."</p>
        Silk Destroying King smiled, "Little Fatty..."<p>"..."</p>
        <p>A sudden silence descended while Zhu Tiandao's face twitched. Silk Destroying King cleared his throat awkwardly. That was a mistake. This was a formal setting, not a private setting where he could call Zhu Tiandao whatever he wanted.</p>
        <p>This was a prefect. It was improper to call him that in a formal setting.</p>
        <p>Realizing his mistake, Silk Destroying King moved on with the topic and said, "So Su Yu has learned the Time technique. How interesting. So be it. I'll give him a book with my own understanding over the years. I hope it won't go to waste in his hands." </p>
        <p>He then tossed a notebook out.</p>
    </div>
    """,
    "expected_output": {
        "chapter_title": "Chapter Y: Little Fatty",
        "paragraphs": [
            'Zhu Tiandao smiled and said, "Half a student is still a student, right? Of course, Silk Destroying King is definitely uninterested in formally acknowledging this student. It\'s fine. Su Yu is quite an arrogant kid as well. He might not be willing-cough, cough. My apologies. I should watch my words. Silk Destroying King, please don\'t mind me..."',
            'Silk Destroying King smiled, "Little Fatty..."',
            '"..."',
            "A sudden silence descended while Zhu Tiandao's face twitched. Silk Destroying King cleared his throat awkwardly. That was a mistake. This was a formal setting, not a private setting where he could call Zhu Tiandao whatever he wanted.",
            "This was a prefect. It was improper to call him that in a formal setting.",
            'Realizing his mistake, Silk Destroying King moved on with the topic and said, "So Su Yu has learned the Time technique. How interesting. So be it. I\'ll give him a book with my own understanding over the years. I hope it won\'t go to waste in his hands."',
            "He then tossed a notebook out."
        ]
    }
}

def run_test_case(test_name: str, test_data: dict):
    """Run a single test case and compare the output."""
    print(f"\n=== Running {test_name} ===")
    
    # Parse the HTML soup
    soup = BeautifulSoup(test_data["soup"], 'html.parser')
    
    # Run the function
    try:
        actual_output = extract_paragraphs_from_soup(soup, test_data["chapter_title_text"])
        
        # Convert to expected format for comparison
        normalized_actual = {
            "chapter_title": actual_output["chapterTitle"],
            "paragraphs": actual_output["content"]
        }
        
        expected_output = test_data["expected_output"]
        
        # Compare outputs
        print(f"Expected: {expected_output}")
        print(f"Actual:   {normalized_actual}")
        
        # Check if they match
        if normalized_actual == expected_output:
            print("✅ TEST PASSED")
            return True
        else:
            print("❌ TEST FAILED")
            print("Differences:")
            if normalized_actual["chapter_title"] != expected_output["chapter_title"]:
                print(f"  Chapter title: expected '{expected_output['chapter_title']}', got '{normalized_actual['chapter_title']}'")
            if normalized_actual["paragraphs"] != expected_output["paragraphs"]:
                print(f"  Paragraphs differ:")
                print(f"    Expected: {expected_output['paragraphs']}")
                print(f"    Actual:   {normalized_actual['paragraphs']}")
            return False
            
    except Exception as e:
        print(f"❌ TEST FAILED WITH EXCEPTION: {e}")
        return False

def run_all_tests():
    """Run all test cases."""
    print("Starting novel content extraction tests...")
    
    test_cases = [
        ("Test Case 1 - Basic paragraphs", test_data1),
        ("Test Case 2 - Mixed content with loose text", test_data2),
        ("Test Case 3 - Paragraphs with emphasized text", test_data3),
        ("Test Case 4 - Complex dialogue with loose text", test_data4),
        ("Test Case 5 - Little Fatty dialogue mixed content", test_data5)
    ]
    
    passed = 0
    total = len(test_cases)
    
    for test_name, test_data in test_cases:
        if run_test_case(test_name, test_data):
            passed += 1
    
    print(f"\n=== TEST SUMMARY ===")
    print(f"Passed: {passed}/{total}")
    print(f"Success rate: {(passed/total)*100:.1f}%")
    
    return passed == total

if __name__ == "__main__":
    run_all_tests()