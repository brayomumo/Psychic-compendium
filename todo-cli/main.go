package main

import (
	"bufio"
	"fmt"
	"log"
	"os"
	"strconv"
)

type Task struct {
	name        string
	description string
	done        bool
}

func newTask(name string, description string) *Task {

	task := Task{name: name, description: description, done: false}
	return &task
}

var tasks []Task

func addTask(scanner bufio.Scanner) int {
	fmt.Println("Add a new task below: ")
	fmt.Println("Task Name: ")

	scanner.Scan()
	// Scan for multiple lines, Scanln for single line
	err := scanner.Err()

	if err != nil {
		log.Fatal(err)
	}
	task := scanner.Text()

	fmt.Println("Task Description: ")
	scanner.Scan()

	err = scanner.Err()
	if err != nil {
		log.Fatal(err)
	}
	desc := scanner.Text()

	tasks = append(tasks, *newTask(task, desc))
	log.Println("Task added successfully")

	return len(tasks)
}

func (t *Task) markAsDone() {
	t.done = true
	tasks = append(tasks, *t)

}

func printTasks() {
	for index, task := range tasks {
		fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.name, task.description, task.done)
	}
}
func printPendingTasks() {
	for index, task := range tasks {
		if !task.done {
			fmt.Printf("%d: name: %s  -> Description: %s --> Done: %t\n", index+1, task.name, task.description, task.done)
		}
	}
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)

	fmt.Println("Hello World\n Welcome to todo-cli")

	// label for the loop
context:
	for {
		fmt.Println("To add new task enter E, to finish a task enter F, to list unfinished tasks enter L, To list All tasks enter A, to quit enter Q")
		scanner.Scan()
		reply := scanner.Text()
		switch reply {
		case "E":
			l := addTask(*scanner)
			fmt.Printf("\nTasks added in array: %d\n ", l)
		case "F":
			fmt.Println("Enter task number to mark as complete:")
			scanner.Scan()
			num := scanner.Text()
			n, err := strconv.Atoi(num)
			if err != nil {
				log.Fatal(err)
			}
			task := tasks[n-1]
			tasks = append(tasks[:n-1], tasks[n:]...)
			task.markAsDone()
			// tasks  = append(tasks, task)

		case "L":
			printPendingTasks()
		case "A":
			printTasks()

		case "Q":
			// break from the for loop using the label
			break context

		}
	}
	// l := addTask(*scanner)
	// fmt.Printf("\nTasks added in array: %d\n ", l)
	// // printTasks()
	// markAsDone(1)
	// printTasks()

}
